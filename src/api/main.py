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

def run_triage(alert: dict, bot_token: str) -> None:
    """Background task: send the alert to the supervisor on AgentCore, then post its answer in the alert's thread."""
    # POC hand-off: this runs inside the container after Slack already got its 200, so a restart here loses the alert.
    # Production: /slack/events puts the alert on SQS and a worker runs this function instead.
    log.info("Triage started for event %s", alert["event_id"])
    try:
        response = _agentcore.invoke_agent_runtime(
            agentRuntimeArn=settings.SUPERVISOR_RUNTIME_ARN,  # deployed ops_supervisor_agent (from .env)
            runtimeSessionId=str(uuid.uuid4()),  # fresh session per alert; 36 characters, the API needs 33+
            payload=json.dumps({"prompt": alert["text"]}).encode("utf-8"),  # same shape supervisor_app.py reads
        )
        result = json.loads(response["response"].read()).get("result", "")  # read the streamed body, take the answer
        text = _THINKING.sub("", result).strip() or "Supervisor returned an empty answer."  # never post a blank message
    except Exception as err:  # any failure must still reach Slack, or the alert looks silently ignored
        log.exception("Triage failed for event %s", alert["event_id"])  # full traceback goes to the logs only
        text = f"Triage failed: {type(err).__name__}. Check the bridge logs."  # error type only; no internals in Slack
    reply = slack.post_message(bot_token, alert["channel"], text, thread_ts=alert["ts"])  # answer under the alert
    if reply.get("ok"):
        log.info("Triage posted for event %s", alert["event_id"])
    else:
        log.error("Slack post failed for event %s: %s", alert["event_id"], reply.get("error"))  # e.g. not_in_channel

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