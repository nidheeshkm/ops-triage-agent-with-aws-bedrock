"""FastAPI bridge: Slack Events API -> ops_supervisor_agent on AgentCore -> reply in the Slack thread."""
import json  # builds the supervisor payload and parses its reply
import logging.config  # logs to stdout; .config applies logging_config.yaml at startup
import re  # removes the <thinking> block if one slips through
import uuid  # makes a unique AgentCore session id per alert
from contextlib import asynccontextmanager  # runs code once at app startup (load secrets)

import boto3  # AWS SDK
from botocore.config import Config  # raises the read timeout for long agent runs
from fastapi import BackgroundTasks, FastAPI, Request  # app, background jobs, raw request access
from fastapi.responses import JSONResponse  # replies with a status code and a JSON body

from config import settings  # .env values and YAML config
from src.api import slack  # Step 12's helpers: secrets, signature, parse_alert, post_message

log = logging.getLogger("ops_bridge")  # named logger for this service

_RUNTIME_CONFIG = settings.environment_config["supervisor_runtime"]  # read_timeout_seconds from YAML
_THINKING = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL)  # safety net; supervisor_app.py already strips it

_session = boto3.Session(profile_name=settings.PROFILE, region_name=settings.REGION)  # your profile locally; IAM role on EC2 or in Kubernetes
_agentcore = _session.client(
    "bedrock-agentcore",  # data-plane client: calls the deployed supervisor
    config=Config(read_timeout=_RUNTIME_CONFIG["read_timeout_seconds"]),  # wait up to 300 s for the triage
)

def _call_supervisor(payload: dict) -> str:
    """Invoke the deployed supervisor with a JSON payload; return its answer with any <thinking> block removed."""
    response = _agentcore.invoke_agent_runtime(
        agentRuntimeArn=settings.SUPERVISOR_RUNTIME_ARN,  # deployed ops_supervisor_agent (from .env)
        runtimeSessionId=str(uuid.uuid4()),  # fresh session per call; 36 characters, the API needs 33+
        payload=json.dumps(payload).encode("utf-8"),  # {"prompt": ...} for triage, plus mode/runbook_id for next steps
    )
    result = json.loads(response["response"].read()).get("result", "")  # read the streamed body, take the answer
    return _THINKING.sub("", result).strip()


def run_triage(alert: dict, bot_token: str) -> None:
    """Background task: triage the alert on AgentCore, then post the answer and a next-steps button in the alert's thread."""
    # POC hand-off: runs inside the container after Slack got its 200, so a restart here loses the alert.
    # Production: /slack/events puts the alert on SQS and a worker runs this function instead.
    log.info("Triage started for event %s", alert["event_id"])
    try:
        text = _call_supervisor({"prompt": alert["text"]}) or "Supervisor returned an empty answer."  # never post a blank message
    except Exception as err:  # any failure must still reach Slack, or the alert looks silently ignored
        log.exception("Triage failed for event %s", alert["event_id"])
        text = f"Triage failed: {type(err).__name__}. Check the bridge logs."  # error type only; no internals in Slack
    reply = slack.post_message(
        bot_token, alert["channel"], slack.to_slack_mrkdwn(text), thread_ts=alert["ts"],  # text: notification and fallback
        blocks=slack.triage_blocks(text, alert["text"]),  # the triage, plus the button when a runbook matched
    )
    if reply.get("ok"):
        log.info("Triage posted for event %s", alert["event_id"])
    else:
        log.error("Slack post failed for event %s: %s", alert["event_id"], reply.get("error"))  # e.g. invalid_blocks

def run_next_steps(click: dict, bot_token: str) -> None:
    """Background task: fetch the full runbook steps through the supervisor and post them in the alert's thread."""
    log.info("Next steps started for %s, requested by %s", click["runbook_id"], click["user"])
    try:
        text = _call_supervisor(  # next-steps mode: supervisor -> ops_agent -> steps assembled by code
            {"mode": "next_steps", "runbook_id": click["runbook_id"], "prompt": click["alert"]}
        ) or "No next steps were returned."
    except Exception as err:  # any failure must still reach Slack, or the click looks ignored
        log.exception("Next steps failed for %s", click["runbook_id"])
        text = f"Next steps failed: {type(err).__name__}. Check the bridge logs."
    reply = slack.post_message(bot_token, click["channel"], slack.to_slack_mrkdwn(text), thread_ts=click["thread_ts"])
    if reply.get("ok"):
        log.info("Next steps posted for %s", click["runbook_id"])
    else:
        log.error("Slack post failed for next steps %s: %s", click["runbook_id"], reply.get("error"))

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs once at startup: set up logging and load the Slack secrets into memory."""
    logging.config.dictConfig(settings.logging_config)  # console logging from logging_config.yaml
    app.state.slack = slack.load_slack_secrets()  # bot_token + signing_secret; startup fails loudly if IAM can't read them
    log.info("Slack secrets loaded; bridge ready")  # never log the secret values themselves
    yield  # the app serves requests while paused here; code after yield would run at shutdown


app = FastAPI(title="ops-supervisor-bridge", lifespan=lifespan)  # the ASGI app that uvicorn runs


@app.get("/health")
def health() -> dict:
    """Liveness and readiness probe for Docker, EC2 checks and Kubernetes."""
    return {"status": "ok"}  # no AWS or Slack calls, so probes stay fast and free

@app.post("/slack/events")
async def slack_events(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """Slack Events API endpoint: verify the request, answer within 3 s, triage in the background."""
    body = await request.body()  # raw bytes: Slack's signature covers these exact bytes
    secrets = request.app.state.slack  # loaded once at startup by lifespan()
    if not slack.verify_slack_signature(
        secrets["signing_secret"],
        request.headers.get("X-Slack-Request-Timestamp", ""),  # when Slack signed it
        body,
        request.headers.get("X-Slack-Signature", ""),  # Slack's signature to compare against
    ):
        log.warning("Rejected request with a bad or old Slack signature")
        return JSONResponse({"error": "invalid signature"}, status_code=401)  # not from Slack: do nothing
    payload = json.loads(body)  # safe to parse now that we know Slack sent it
    if payload.get("type") == "url_verification":  # Slack's one-time check when you save the Request URL
        return JSONResponse({"challenge": payload.get("challenge")})  # echo the challenge back to prove we own the URL
    if request.headers.get("X-Slack-Retry-Reason") == "http_timeout":  # Slack resending one we probably already accepted
        log.info("Ignored Slack retry after timeout for event %s", payload.get("event_id"))
        return JSONResponse({"ok": True})  # POC dedupe; production checks event_id in DynamoDB instead
    alert = slack.parse_alert(payload)  # None for edits, joins, thread replies, other channels
    if alert:
        background_tasks.add_task(run_triage, alert, secrets["bot_token"])  # starts after the response below is sent
        log.info("Accepted alert %s", alert["event_id"])
    return JSONResponse({"ok": True})  # 200 fast; Slack retries anything slower than 3 s

@app.post("/slack/interactions")
async def slack_interactions(request: Request, background_tasks: BackgroundTasks) -> JSONResponse:
    """Slack interactivity endpoint: verify the click, answer within 3 s, fetch next steps in the background."""
    body = await request.body()  # raw bytes: the signature covers these exact bytes
    secrets = request.app.state.slack
    if not slack.verify_slack_signature(
        secrets["signing_secret"],
        request.headers.get("X-Slack-Request-Timestamp", ""),
        body,
        request.headers.get("X-Slack-Signature", ""),
    ):
        log.warning("Rejected interaction with a bad or old Slack signature")
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    click = slack.parse_next_steps_click(body)  # None for other buttons, other channels, other interaction types
    if click:
        note = {"type": "context", "elements": [{"type": "mrkdwn", "text": f"Next steps requested by <@{click['user']}>, working on it…"}]}
        blocks = [b for b in (click["section_block"],) if b] + [note]  # keep the triage text, replace the button
        background_tasks.add_task(  # runs first: removes the button so a second click can't start a second run
            slack.update_message, secrets["bot_token"], click["channel"], click["message_ts"], click["triage_text"], blocks
        )
        background_tasks.add_task(run_next_steps, click, secrets["bot_token"])  # then the slow part
        log.info("Accepted next-steps click for %s", click["runbook_id"])
    return JSONResponse({"ok": True})  # Slack needs a 200 within 3 s, or it shows the user an error