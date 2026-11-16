import logging.config  # applies the logging settings from logging_config.yaml

import boto3  # AWS SDK, used to build a session from our profile
from strands import Agent  # the agent class: runs the model-and-tools loop
from strands.models import BedrockModel  # Strands wrapper that talks to Bedrock models

from config import settings  # loads .env and the YAML files (see config/settings.py)
from src.tools.ops_tools import get_active_alerts, get_recent_deployments, search_runbooks  # our three read-only tools

# profile is None in the cloud, so boto3 falls back to the runtime's IAM role
session = boto3.Session(profile_name=settings.PROFILE, region_name=settings.REGION)

model = BedrockModel(
    model_id=settings.MODEL_ID,  # from .env
    boto_session=session,
    temperature=settings.model_config["temperature"],  # from model_config.yaml
    guardrail_id=settings.GUARDRAIL_ID,  # from .env
    guardrail_version=settings.GUARDRAIL_VERSION,  # from .env: pinned numbered version
    guardrail_trace=settings.model_config["guardrail_trace"],  # from model_config.yaml
    guardrail_latest_message=settings.model_config["guardrail_latest_message"],  # from model_config.yaml
)


def build_agent() -> Agent:
    return Agent(
        model=model,
        system_prompt=settings.agent_config["ops_agent"]["system_prompt"],  # from agent_config.yaml
        tools=[get_recent_deployments, get_active_alerts, search_runbooks],
    )


if __name__ == "__main__":
    logging.config.dictConfig(settings.logging_config)  # set up logging only when run directly
    agent = build_agent()
    agent("The checkout service has an alert firing. What is the most likely cause, and what should I do?")