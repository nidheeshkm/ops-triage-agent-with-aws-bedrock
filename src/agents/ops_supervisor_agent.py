"""ops_supervisor_agent: reads a Slack alert, rates it, and asks ops_agent for evidence when needed."""
import logging.config  # applies logging_config.yaml; used by the __main__ block in Step 6

import boto3  # AWS SDK; builds the session the model uses
from strands import Agent  # the agent loop: model + prompt + tools; used in Step 5c
from strands.models import BedrockModel  # Bedrock model wrapper with guardrail support

from config import settings  # .env values and YAML config
from src.tools.supervisor_tools import ask_ops_agent  # Step 4's tool; given to the agent in Step 5c

session = boto3.Session(profile_name=settings.PROFILE, region_name=settings.REGION)  # your profile locally; IAM role on AgentCore
model = BedrockModel(
    model_id=settings.MODEL_ID,  # amazon.nova-pro-v1:0 from .env
    boto_session=session,  # the session above; region and credentials
    temperature=settings.model_config["temperature"],  # 0.2 from YAML: steady, repeatable triage
    guardrail_id=settings.GUARDRAIL_ID,  # same guardrail as ops_agent
    guardrail_version=settings.GUARDRAIL_VERSION,  # the numbered version, not DRAFT
    guardrail_trace=settings.model_config["guardrail_trace"],  # log why the guardrail acted
    guardrail_latest_message=settings.model_config["guardrail_latest_message"],  # check only the newest message
)
def build_agent() -> Agent:
    """Return a fresh ops_supervisor_agent. Build one per alert so alerts never share conversation history."""
    return Agent(
        model=model,  # Nova Pro + guardrail, defined above
        system_prompt=settings.agent_config["ops_supervisor_agent"]["system_prompt"],  # router prompt from YAML
        tools=[ask_ops_agent],  # its only tool for now; future specialist agents get added to this list
    )

if __name__ == "__main__":
    logging.config.dictConfig(settings.logging_config)  # console logging set up from logging_config.yaml
    sample_alert = (  # shaped like an alert a monitoring tool would post in Slack
        "[FIRING] checkout: payment error rate 12% (threshold 2%) for 10 minutes. "
        "Region ap-southeast-2. Started 2026-11-14T10:15Z."
    )
    result = build_agent()(sample_alert)  # build a fresh agent and run it on this one alert
    print("\n--- supervisor reply ---\n", result)  # str(result) is the agent's final answer text