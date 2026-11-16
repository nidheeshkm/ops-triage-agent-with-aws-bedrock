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


@tool
def ask_ops_agent(question: str) -> str:
    """Ask the ops_agent specialist about a service's recent deployments, active alerts or runbooks.

    Use this when an alert needs evidence: what changed recently, what else is firing,
    or which runbook applies. Ask one clear question that names the service and the symptom.

    Args:
        question: A complete question for ops_agent, naming the service and the symptom.
    """
    try:
        response = _agentcore.invoke_agent_runtime(
            agentRuntimeArn=settings.OPS_AGENT_RUNTIME_ARN,  # which deployed runtime to call (from .env)
            runtimeSessionId=str(uuid.uuid4()),  # fresh session per question; the API needs 33+ characters, a UUID has 36
            payload=json.dumps({"prompt": question}).encode("utf-8"),  # same shape app.py's entrypoint reads
        )
        body = json.loads(response["response"].read())  # read the streamed reply body, then parse its JSON
        return body.get("result", str(body))  # app.py returns {"result": "..."}; fall back to the raw body
    except (ClientError, BotoCoreError, ValueError) as err:  # AWS error, network error, or a reply that isn't JSON
        return f"ops_agent call failed: {err}"  # tell the supervisor instead of crashing it
