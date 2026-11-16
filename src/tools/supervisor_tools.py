"""Tools for ops_supervisor_agent."""
import json  # builds the request payload and parses ops_agent's JSON reply
import uuid  # makes a unique session id for each call

import boto3  # AWS SDK
from botocore.config import Config  # client settings; used to raise the read timeout
from botocore.exceptions import BotoCoreError, ClientError  # AWS API errors and network errors
from strands import tool  # turns a function into a tool the agent can call

from config import settings  # .env values and YAML config

_RUNTIME_CONFIG = settings.environment_config["ops_agent_runtime"]  # read_timeout_seconds from YAML

_session = boto3.Session(profile_name=settings.PROFILE, region_name=settings.REGION)  # your profile locally; IAM role on AgentCore
_agentcore = _session.client(
    "bedrock-agentcore",  # data-plane client: calls deployed runtimes
    config=Config(read_timeout=_RUNTIME_CONFIG["read_timeout_seconds"]),  # wait up to 120 s for ops_agent's reply
)


def _invoke_ops_agent(payload: dict) -> str:
    """Call the deployed ops_agent with a JSON payload; return its "result" text, or an error message."""
    try:
        response = _agentcore.invoke_agent_runtime(
            agentRuntimeArn=settings.OPS_AGENT_RUNTIME_ARN,  # which deployed runtime to call (from .env)
            runtimeSessionId=str(uuid.uuid4()),  # fresh session per call; the API needs 33+ characters, a UUID has 36
            payload=json.dumps(payload).encode("utf-8"),  # JSON body for ops_agent's app.py
        )
        body = json.loads(response["response"].read())  # read the streamed reply body, then parse its JSON
        return body.get("result", str(body))  # app.py returns {"result": "..."}; fall back to the raw body
    except (ClientError, BotoCoreError, ValueError) as err:  # AWS error, network error, or a reply that isn't JSON
        return f"ops_agent call failed: {err}"  # report it instead of crashing the caller


@tool
def ask_ops_agent(question: str) -> str:
    """Ask the ops_agent specialist about a service's recent deployments, active alerts or runbooks.

    Use this when an alert needs evidence: what changed recently, what else is firing,
    or which runbook applies. Ask one clear question that names the service and the symptom.

    Args:
        question: A complete question for ops_agent, naming the service and the symptom.
    """
    return _invoke_ops_agent({"prompt": question})  # normal mode: ops_agent runs its agent loop


def request_next_steps(runbook_id: str, alert: str) -> str:
    """Ask ops_agent for the full, code-assembled next steps of one runbook. Used by the button, not by the model."""
    return _invoke_ops_agent({"mode": "next_steps", "runbook_id": runbook_id, "prompt": alert})
