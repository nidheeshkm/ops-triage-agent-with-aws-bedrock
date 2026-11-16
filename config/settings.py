import os  # read environment variables
from pathlib import Path  # build file paths that work from any working directory

import yaml  # parse the YAML config files
from dotenv import load_dotenv  # read .env into environment variables

load_dotenv()  # must run before any value below is read

CONFIG_DIR = Path(__file__).parent  # the folder this file lives in, so paths don't depend on where you run Python


def _load_yaml(filename):  # leading underscore: internal helper, not part of the module's public API
    with open(CONFIG_DIR / filename, encoding="utf-8") as f:  # open the file next to this module
        return yaml.safe_load(f)  # parse it into plain dicts, lists and values


# behavior settings, from the YAML files
agent_config = _load_yaml("agent_config.yaml")
model_config = _load_yaml("model_config.yaml")
environment_config = _load_yaml("environment_config.yaml")
logging_config = _load_yaml("logging_config.yaml")

# environment-specific values, from .env (None when the variable is not set)
PROFILE = os.getenv("AWS_PROFILE")  # unset in the cloud, where the IAM role is used
REGION = os.getenv("AWS_REGION")
MODEL_ID = os.getenv("MODEL_ID")
KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID")
RUNBOOK_BUCKET = os.getenv("RUNBOOK_BUCKET")  # S3 bucket holding the runbook PDFs; get_runbook reads whole files from it
GUARDRAIL_ID = os.getenv("GUARDRAIL_ID")
GUARDRAIL_VERSION = os.getenv("GUARDRAIL_VERSION")
OPS_AGENT_RUNTIME_ARN = os.getenv("OPS_AGENT_RUNTIME_ARN")  # deployed ops_agent; the supervisor's tool calls it
SLACK_SECRET_ID = os.getenv("SLACK_SECRET_ID")  # Secrets Manager name holding bot_token + signing_secret; read by FastAPI
SLACK_ALERT_CHANNEL_ID = os.getenv("SLACK_ALERT_CHANNEL_ID")  # only messages from this Slack channel are triaged
SUPERVISOR_RUNTIME_ARN = os.getenv("SUPERVISOR_RUNTIME_ARN")  # deployed ops_supervisor_agent that FastAPI calls