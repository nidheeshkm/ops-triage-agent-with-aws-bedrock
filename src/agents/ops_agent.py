import logging.config  # applies the logging settings from logging_config.yaml
import re  # removes a <thinking> block from the matcher's reply

from botocore.exceptions import BotoCoreError, ClientError  # S3 and network errors from the runbook loader
import boto3  # AWS SDK, used to build a session from our profile
from strands import Agent  # the agent class: runs the model-and-tools loop
from strands.models import BedrockModel  # Strands wrapper that talks to Bedrock models

from config import settings  # loads .env and the YAML files (see config/settings.py)
from src.tools.ops_tools import get_active_alerts, get_recent_deployments, search_runbooks  # our three read-only tools
from src.tools.runbook_tools import _load_runbook, get_runbook, get_runbook_info  # loader reused by next_steps_reply

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
        tools=[get_recent_deployments, get_active_alerts, search_runbooks, get_runbook_info, get_runbook],  # runbook tools added in Phase 3
    )

def next_steps_reply(runbook_id: str, alert: str) -> str:
    """Full next-steps answer: runbook steps copied by code; the model only matches the Diagnosis row."""
    try:
        rb = _load_runbook(runbook_id.strip().upper())  # same loader and checks the runbook tools use
    except (ValueError, ClientError, BotoCoreError) as err:
        return f"Runbook lookup failed: {err}"
    sections = rb["sections"]  # {heading: [steps]} built by code

    diagnosis = "\n".join(sections.get("Diagnosis", []))  # the table as loose cell text
    match = "NO MATCH"
    if diagnosis:
        matcher = Agent(  # tiny agent: no tools, sees only the Diagnosis table and the alert
            model=model,  # same Nova Pro model and guardrail as ops_agent
            system_prompt=settings.agent_config["ops_agent_symptom_matcher"]["system_prompt"],
            tools=[],
            callback_handler=None,  # don't stream the matcher's text to the logs
        )
        reply = str(matcher(f"Alert: {alert}\n\nDiagnosis table:\n{diagnosis}"))
        match = re.sub(r"<thinking>.*?</thinking>\s*", "", reply, flags=re.DOTALL).strip()  # keep only the answer

    lines = []  # warnings first, so a runbook problem is the first thing on-call reads
    if rb["outdated"]:
        lines.append(f"RUNBOOK WARNING: {rb['runbook_id']} may be outdated: last reviewed {rb['last_reviewed'] or 'unknown'} "
                     f"(limit {settings.environment_config['runbooks']['max_age_days']} days). Please review it.")
    if rb["missing_sections"]:
        lines.append(f"RUNBOOK WARNING: {rb['runbook_id']} is missing sections: {', '.join(rb['missing_sections'])}. Please add them.")
    if match.upper().startswith("NO MATCH"):  # tolerate "No match." or extra words after it
        lines.append(f"RUNBOOK WARNING: no Diagnosis row in {rb['runbook_id']} matches this alert. Please add one.")
    lines.append(f"**Runbook:** {rb['runbook_id']} - {rb['title']} "
                 f"(default {rb['default_severity'] or 'severity not stated'}, last reviewed {rb['last_reviewed'] or 'unknown'})")
    if not match.upper().startswith("NO MATCH"):  # only show a row when the matcher found one
        lines.append("**Matched symptom:**\n" + match)
    for name in ("First 5 minutes", "Mitigation", "Escalation", "Verify recovery"):  # copied word for word, in order
        if sections.get(name):
            lines.append(f"**{name}:**\n" + "\n".join(sections[name]))
    return "\n\n".join(lines)

if __name__ == "__main__":
    logging.config.dictConfig(settings.logging_config)  # set up logging only when run directly
    agent = build_agent()
    agent("The checkout service has an alert firing. What is the most likely cause, and what should I do?")