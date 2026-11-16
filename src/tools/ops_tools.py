from datetime import datetime, timedelta, timezone  # real timestamps, always in UTC

import boto3  # AWS SDK, used to query the Knowledge Base
from botocore.exceptions import ClientError  # error AWS raises when a call is rejected
from strands import tool  # decorator that turns a Python function into a tool the agent can call

from config import settings  # profile, region, Knowledge Base ID and limits (loads .env itself)

_KB_CONFIG = settings.environment_config["knowledge_base"]  # search limits from environment_config.yaml
_DEPLOY_CONFIG = settings.environment_config["deployments"]  # look-back window from environment_config.yaml
def _hours_ago(hours: float) -> datetime:
    # Sample-data helper: a UTC time N hours before now, so the demo data always looks fresh
    return datetime.now(timezone.utc) - timedelta(hours=hours)


# Sample data standing in for a real deploy system (no real integration yet)
SAMPLE_DEPLOYMENTS = {
    "checkout": [
        {"version": "2.14.0", "deployed_at": _hours_ago(2), "change": "Lowered payment gateway timeout from 5s to 2s"},
        {"version": "2.13.4", "deployed_at": _hours_ago(30), "change": "Fixed cart rounding bug"},
    ],
    "search": [
        {"version": "5.2.1", "deployed_at": _hours_ago(72), "change": "Reindexed product catalog"},
    ],
}
# Sample alerts (no real monitoring integration yet); the checkout alert starts ~15 min after its deploy
SAMPLE_ALERTS = {
    "checkout": [
        {"started_at": _hours_ago(1.75), "severity": "critical", "name": "Checkout error rate above 5%",
         "detail": "HTTP 504 from payment gateway, 18% of requests failing"},
    ],
    "search": [],  # healthy: no active alerts
}


@tool  # registers this function as an agent tool
def get_recent_deployments(service: str, hours: int = _DEPLOY_CONFIG["default_hours"]) -> str:
    """List recent deployments for a service, newest first, with exact UTC deploy times.

    Compare these times with the alert start time to see whether a deploy came just before it.

    Args:
        service: Service name, for example "checkout" or "search".
        hours: How far back to look, in hours. Defaults to the configured look-back window.
    """
    deploys = SAMPLE_DEPLOYMENTS.get(service)  # None if we don't know the service
    if deploys is None:
        return f"Unknown service '{service}'. Known services: {', '.join(SAMPLE_DEPLOYMENTS)}."
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)  # oldest deploy time we still show
    recent = [d for d in deploys if d["deployed_at"] >= cutoff]
    recent.sort(key=lambda d: d["deployed_at"], reverse=True)  # newest first
    if not recent:
        return f"No deployments for '{service}' in the last {hours} hours."
    # ISO 8601 UTC, like 2026-11-14T01:37:05Z: the same format most alert tools use
    lines = [f"{d['deployed_at']:%Y-%m-%dT%H:%M:%SZ} v{d['version']}: {d['change']}" for d in recent]
    return "\n".join(lines)  # plain text is easy for the model to read

@tool  # registers this function as an agent tool
def get_active_alerts(service: str) -> str:
    """List currently firing alerts for a service, with exact UTC start times.

    Use the start time to check whether it lines up with a recent deployment.

    Args:
        service: Service name, for example "checkout" or "search".
    """
    alerts = SAMPLE_ALERTS.get(service)  # None if we don't know the service
    if alerts is None:
        return f"Unknown service '{service}'. Known services: {', '.join(SAMPLE_ALERTS)}."
    if not alerts:
        return f"No active alerts for '{service}'."
    lines = [
        f"{a['started_at']:%Y-%m-%dT%H:%M:%SZ} [{a['severity']}] {a['name']}: {a['detail']}"
        for a in alerts
    ]
    return "\n".join(lines)  # same plain-text, UTC format as the deploy tool

@tool  # registers this function as an agent tool
def search_runbooks(query: str, max_results: int = _KB_CONFIG["default_results"]) -> str:
    """Search the team's ops runbooks and return the most relevant passages.

    Use this for how-to guidance: triage steps, likely causes, mitigation and escalation
    for incidents such as outages, slow pages, failed jobs, login, cart or payment problems.

    Args:
        query: What to look up, in plain language, for example "payment gateway timeouts after a deployment".
        max_results: How many passages to return, from 1 up to the configured maximum.
    """
    kb_id = settings.KNOWLEDGE_BASE_ID  # from .env, loaded by config.settings
    if not kb_id:
        return "Runbook search is not configured: KNOWLEDGE_BASE_ID is missing."
    max_results = max(1, min(max_results, _KB_CONFIG["max_results"]))  # keep within the configured limit
    session = boto3.Session(profile_name=settings.PROFILE, region_name=settings.REGION)  # profile is None in the cloud, so the IAM role is used
    client = session.client("bedrock-agent-runtime")  # the client that queries Knowledge Bases
    try:
        response = client.retrieve(  # same call as the CLI test in Step 11b
            knowledgeBaseId=kb_id,
            retrievalQuery={"text": query},
            retrievalConfiguration={"vectorSearchConfiguration": {"numberOfResults": max_results}},
        )
    except ClientError as err:
        return f"Runbook search failed: {err.response['Error']['Code']}"  # a clean message, not a crash
    results = response.get("retrievalResults", [])
    if not results:
        return "No matching runbook content found."
    blocks = []
    for item in results:
        uri = item.get("location", {}).get("s3Location", {}).get("uri", "unknown")
        filename = uri.rsplit("/", 1)[-1]  # keep only the PDF name, so the model can cite the runbook
        blocks.append(f"[{filename}] (score {item.get('score', 0):.2f})\n{item['content']['text']}")
    return "\n\n---\n\n".join(blocks)  # separator between passages