"""Slack helpers for the FastAPI bridge: secrets, signature check, event parsing, thread replies."""
import hashlib  # SHA-256, the hash Slack signs requests with
import hmac  # keyed hashing and a constant-time compare for the signature check
import json  # parses the secret and Slack's JSON payloads
import re  # converts Markdown bold to Slack bold and finds the runbook ID
import time  # rejects old requests (replay protection)
import urllib.request  # calls Slack's chat.postMessage without adding a package
import urllib.parse  # decodes the form body Slack sends for button clicks
import boto3  # AWS SDK; reads the Slack secret

from config import settings  # .env values: SLACK_SECRET_ID, SLACK_ALERT_CHANNEL_ID, profile, region

_session = boto3.Session(profile_name=settings.PROFILE, region_name=settings.REGION)  # your profile locally; IAM role on EC2 or in Kubernetes
_secrets = _session.client("secretsmanager")  # Secrets Manager client; used by load_slack_secrets() in Step 12b

def load_slack_secrets() -> dict:
    """Fetch {"bot_token": ..., "signing_secret": ...} from Secrets Manager. Call once when the app starts."""
    response = _secrets.get_secret_value(SecretId=settings.SLACK_SECRET_ID)  # IAM decides whether this caller may read it
    return json.loads(response["SecretString"])  # the console stored the key/value pairs as one JSON string

def verify_slack_signature(signing_secret: str, timestamp: str, body: bytes, signature: str) -> bool:
    """True only if Slack signed this exact body within the last 5 minutes."""
    try:
        if abs(time.time() - int(timestamp)) > 300:  # older than 5 minutes: likely a replayed request
            return False
    except (TypeError, ValueError):  # X-Slack-Request-Timestamp header missing or not a number
        return False
    base = b"v0:" + timestamp.encode() + b":" + body  # Slack's signing string: version, timestamp, raw body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()  # the signature Slack would send
    return hmac.compare_digest(expected, signature or "")  # constant-time compare, so timing reveals nothing

_ALLOWED_SUBTYPES = {None, "bot_message"}  # plain posts by people and posts by monitoring integrations


def parse_alert(payload: dict) -> dict | None:
    """Return the alert to triage from a Slack event payload, or None if this event must be ignored."""
    if payload.get("type") != "event_callback":  # url_verification and other envelope types are not alerts
        return None
    event = payload.get("event", {})  # the actual Slack event inside the envelope
    if event.get("type") != "message" or event.get("subtype") not in _ALLOWED_SUBTYPES:  # skip edits, deletes, channel joins
        return None
    if event.get("channel") != settings.SLACK_ALERT_CHANNEL_ID:  # only the configured alert channel
        return None
    if event.get("thread_ts") not in (None, event.get("ts")):  # a thread reply, including the supervisor's own: never re-triage
        return None
    text = (event.get("text") or "").strip()  # the alert text; empty if missing
    if not text:  # nothing to analyse
        return None
    return {"event_id": payload.get("event_id"), "channel": event["channel"], "ts": event["ts"], "text": text}

def post_message(bot_token: str, channel: str, text: str, thread_ts: str | None = None, blocks: list | None = None) -> dict:
    """Post text to a Slack channel, as a thread reply when thread_ts is given; return Slack's JSON reply."""
    body = {"channel": channel, "text": text}  # where and what to post
    if thread_ts:  # reply under the alert instead of as a new channel message
        body["thread_ts"] = thread_ts
    if blocks:  # rich layout (buttons); `text` stays as the notification and fallback text
        body["blocks"] = blocks
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",  # Slack Web API method for posting messages
        data=json.dumps(body).encode("utf-8"),  # JSON body as bytes
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8"},  # bot token auth
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # 10 s cap so a slow Slack can't hang the worker
        return json.loads(response.read())  # {"ok": true, "ts": ...} on success, {"ok": false, "error": ...} on failure

_MACHINE_LINES = re.compile(r"^(RUNBOOK_ID|DEFAULT_SEVERITY):.*\n?", re.M)  # lines meant for code, not people


def to_slack_mrkdwn(text: str) -> str:
    """Convert an agent's Markdown answer to Slack mrkdwn and drop machine-only lines."""
    text = _MACHINE_LINES.sub("", text)  # remove RUNBOOK_ID / DEFAULT_SEVERITY if an agent echoed them
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)  # **bold** -> *bold*
    text = re.sub(r"^RUNBOOK WARNING:", ":warning: *Runbook warning:*", text, flags=re.M)  # warnings stand out in Slack
    return text.strip()


NEXT_STEPS_ACTION = "next_steps"  # action_id that the /slack/interactions endpoint listens for
_RUNBOOK_LINE = re.compile(r"^Runbook:\s*(RB-\d{3})\s*$", re.M)  # the triage reply's fixed last line


def runbook_id_of(text: str) -> str | None:
    """Return the RB-NNN from a triage reply's 'Runbook:' line, or None if no runbook matched."""
    found = _RUNBOOK_LINE.search(text)
    return found.group(1) if found else None


def triage_blocks(text: str, alert: str) -> list:
    """Block Kit layout: the triage text, plus a 'Help me with next steps' button when a runbook matched."""
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": to_slack_mrkdwn(text)[:3000]}}]  # a section holds at most 3000 characters
    runbook_id = runbook_id_of(text)
    if runbook_id:  # no runbook, no button: there would be nothing to fetch
        blocks.append({
            "type": "actions",
            "elements": [{
                "type": "button",
                "action_id": NEXT_STEPS_ACTION,
                "text": {"type": "plain_text", "text": "Help me with next steps"},
                "value": json.dumps({"runbook_id": runbook_id, "alert": alert[:1500]}),  # everything the click needs; Slack allows 2000 characters
            }],
        })
    return blocks

def parse_next_steps_click(body: bytes) -> dict | None:
    """Return the details of a 'Help me with next steps' click from the alert channel, or None for anything else."""
    form = urllib.parse.parse_qs(body.decode("utf-8"))  # Slack sends payload=<json> as a URL-encoded form
    payload = json.loads(form.get("payload", ["{}"])[0])  # the interaction itself
    if payload.get("type") != "block_actions":  # only button clicks; ignore other interaction types
        return None
    action = next((a for a in payload.get("actions", []) if a.get("action_id") == NEXT_STEPS_ACTION), None)
    channel = payload.get("channel", {}).get("id")
    if not action or channel != settings.SLACK_ALERT_CHANNEL_ID:  # another button, or another channel
        return None
    value = json.loads(action.get("value") or "{}")  # runbook_id + alert, set by triage_blocks()
    message = payload.get("message", {})  # the bot's triage reply that holds the button
    return {
        "runbook_id": value.get("runbook_id", ""),
        "alert": value.get("alert", ""),
        "channel": channel,
        "message_ts": message.get("ts"),  # the triage reply, whose button gets replaced
        "thread_ts": message.get("thread_ts") or message.get("ts"),  # the alert's thread, where the steps go
        "triage_text": message.get("text", ""),  # kept as the fallback text when the reply is updated
        "section_block": (message.get("blocks") or [None])[0],  # the triage text block, kept as-is
        "user": payload.get("user", {}).get("id"),  # who clicked, for the "requested by" note
    }


def update_message(bot_token: str, channel: str, ts: str, text: str, blocks: list) -> dict:
    """Replace an existing bot message's content, here to swap the button for a 'requested by' note."""
    request = urllib.request.Request(
        "https://slack.com/api/chat.update",  # Slack Web API method for editing a message
        data=json.dumps({"channel": channel, "ts": ts, "text": text, "blocks": blocks}).encode("utf-8"),
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # same 10 s cap as post_message
        return json.loads(response.read())  # {"ok": true, ...} or {"ok": false, "error": ...}