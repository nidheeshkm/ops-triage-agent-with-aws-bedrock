from bedrock_agentcore.runtime import BedrockAgentCoreApp  # AgentCore's HTTP wrapper: serves /invocations and /ping

from src.agents.ops_supervisor_agent import build_agent  # the supervisor from Steps 5b and 5c
import re  # regular expressions; used to remove Nova's <thinking> block
app = BedrockAgentCoreApp()  # one app per runtime; this container serves only the supervisor
_THINKING = re.compile(r"<thinking>.*?</thinking>\s*", re.DOTALL)  # Nova's reasoning block; DOTALL lets . match line breaks

@app.entrypoint
def invoke(payload):
    """AgentCore calls this once per request; payload is the JSON body sent to invoke_agent_runtime."""
    agent = build_agent()  # fresh agent per alert, so alerts never share history
    result = agent(payload.get("prompt", ""))  # the alert text; empty string if the key is missing
    return {"result": _THINKING.sub("", str(result)).strip()}  # drop the reasoning block, then trim leftover whitespace


if __name__ == "__main__":
    app.run()  # local server on port 8080, for testing before deploy