"""Send Slack-style signed requests to the local bridge, to test /slack/events without Slack."""
import hashlib  # SHA-256 for the signature
import hmac  # keyed hash, the way Slack signs
import json  # builds the request bodies
import os  # reads BRIDGE_URL so the same script can test localhost or API Gateway
import sys  # reads which test case to run
import time  # signature timestamp and response timing
import urllib.error  # catches 4xx/5xx replies so they print instead of crashing
import urllib.parse  # builds the form body Slack uses for button clicks
import urllib.request  # sends the HTTP request

from config import settings  # alert channel ID from .env
from src.api.slack import load_slack_secrets, post_message, to_slack_mrkdwn, triage_blocks  # secrets, posting, the button layout

URL = os.getenv("BRIDGE_URL", "http://localhost:8000/slack/events")  # override to test through API Gateway
INTERACTIONS_URL = os.getenv("BRIDGE_INTERACTIONS_URL", "http://localhost:8000/slack/interactions")  # button-click endpoint


def send(payload: dict, secret: str) -> None:
    """Sign the payload like Slack does, POST it, and print status, body and response time."""
    body = json.dumps(payload).encode("utf-8")
    ts = str(int(time.time()))  # Slack's X-Slack-Request-Timestamp
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()  # same formula as the bridge checks
    req = urllib.request.Request(URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.status, resp.read().decode(), f"{time.time() - start:.2f}s")
    except urllib.error.HTTPError as err:  # 401 and other error replies
        print(err.code, err.read().decode(), f"{time.time() - start:.2f}s")

def send_click(click: dict, secret: str) -> None:
    """Sign a button click like Slack does (a form with a JSON 'payload' field), POST it, print the result."""
    body = urllib.parse.urlencode({"payload": json.dumps(click)}).encode("utf-8")  # Slack's form encoding
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(secret.encode(), b"v0:" + ts.encode() + b":" + body, hashlib.sha256).hexdigest()  # same formula as for events
    req = urllib.request.Request(INTERACTIONS_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded", "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig})
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.status, resp.read().decode(), f"{time.time() - start:.2f}s")
    except urllib.error.HTTPError as err:
        print(err.code, err.read().decode(), f"{time.time() - start:.2f}s")


if __name__ == "__main__":
    case = sys.argv[1] if len(sys.argv) > 1 else "alert"  # challenge | forged | alert
    secrets = load_slack_secrets()
    if case == "challenge":  # Slack's URL check from Step 18
        send({"type": "url_verification", "challenge": "test-challenge-123"}, secrets["signing_secret"])
    elif case == "forged":  # an attacker without the signing secret
        send({"type": "event_callback", "event": {}}, "wrong-secret")
    elif case == "click":  # a 'Help me with next steps' click on a ready-made triage reply, with no triage cost
        text = "[FIRING] Business Manager job CatalogImport ended with status ERROR at step ImportCatalog: file not found on WebDAV."
        alert = post_message(secrets["bot_token"], settings.SLACK_ALERT_CHANNEL_ID, text)  # the alert, top-level
        triage = "Summary: CatalogImport failed: feed file not found on WebDAV.\nSeverity: High\nRunbook: RB-003"
        blocks = triage_blocks(triage, text)  # the same layout run_triage posts, button included
        reply = post_message(secrets["bot_token"], settings.SLACK_ALERT_CHANNEL_ID, to_slack_mrkdwn(triage), alert["ts"], blocks)
        send_click({  # what Slack sends when someone clicks that button
            "type": "block_actions", "user": {"id": "U0TEST"}, "channel": {"id": settings.SLACK_ALERT_CHANNEL_ID},
            "message": {"ts": reply["ts"], "thread_ts": alert["ts"], "text": to_slack_mrkdwn(triage), "blocks": blocks},
            "actions": [blocks[1]["elements"][0]],
        }, secrets["signing_secret"])
    else:  # a real alert: post it to Slack first, so the reply has a real thread to land in
        text = "[FIRING] checkout: payment error rate 12% (threshold 2%) for 10 minutes. Region ap-southeast-2."
        msg = post_message(secrets["bot_token"], settings.SLACK_ALERT_CHANNEL_ID, text)
        send({"type": "event_callback", "event_id": "EvLocal1", "event": {
            "type": "message", "channel": settings.SLACK_ALERT_CHANNEL_ID, "ts": msg["ts"], "text": text}}, secrets["signing_secret"])