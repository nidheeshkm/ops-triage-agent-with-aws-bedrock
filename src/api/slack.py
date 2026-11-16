"""Slack helpers for the FastAPI bridge: secrets, signature check, event parsing, thread replies."""
import hashlib  # SHA-256, the hash Slack signs requests with
import hmac  # keyed hashing and a constant-time compare for the signature check
import json  # parses the secret and Slack's JSON payloads
import time  # rejects old requests (replay protection)
import urllib.request  # calls Slack's chat.postMessage without adding a package

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

def post_message(bot_token: str, channel: str, text: str, thread_ts: str | None = None) -> dict:
    """Post text to a Slack channel, as a thread reply when thread_ts is given; return Slack's JSON reply."""
    body = {"channel": channel, "text": text}  # where and what to post
    if thread_ts:  # reply under the alert instead of as a new channel message
        body["thread_ts"] = thread_ts
    request = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",  # Slack Web API method for posting messages
        data=json.dumps(body).encode("utf-8"),  # JSON body as bytes
        headers={"Authorization": f"Bearer {bot_token}", "Content-Type": "application/json; charset=utf-8"},  # bot token auth
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:  # 10 s cap so a slow Slack can't hang the worker
        return json.loads(response.read())  # {"ok": true, "ts": ...} on success, {"ok": false, "error": ...} on failure