from bedrock_agentcore.runtime import BedrockAgentCoreApp  # AgentCore's HTTP wrapper: serves /invocations and /ping

from src.agents.ops_supervisor_agent import build_agent  # the supervisor from Steps 5b and 5c
from src.tools.supervisor_tools import request_next_steps  # button path: structured next-steps call to ops_agent
import re  # regular expressions; used to remove Nova's <thinking> block
app = BedrockAgentCoreApp()  # one app per runtime; this container serves only the supervisor
_THINKING = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL)  # Nova's reasoning block; DOTALL lets . match line breaks

@app.entrypoint
def invoke(payload):
    """AgentCore calls this once per request. mode "triage" (default) runs the supervisor; "next_steps" fetches full runbook steps."""
    if payload.get("mode") == "next_steps":  # button click: the action is fixed, so skip the supervisor's model
        result = request_next_steps(payload.get("runbook_id", ""), payload.get("prompt", ""))  # ops_agent assembles the steps
    else:
        agent = build_agent()  # fresh agent per alert, so alerts never share history
        result = agent(payload.get("prompt", ""))  # triage: rate the alert, call ops_agent only if needed
    return {"result": _THINKING.sub("", str(result)).strip()}  # same clean-up and reply shape for both modes


if __name__ == "__main__":
    app.run()  # local server on port 8080, for testing before deploy