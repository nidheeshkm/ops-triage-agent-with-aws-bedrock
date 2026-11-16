from bedrock_agentcore.runtime import BedrockAgentCoreApp  # AgentCore's wrapper: turns our code into an HTTP service

from src.agents.ops_agent import build_agent, next_steps_reply  # next_steps_reply: steps assembled by code
app = BedrockAgentCoreApp()  # creates the web app that AgentCore will call


@app.entrypoint
def invoke(payload):
    """mode "next_steps": full runbook steps assembled by code. Anything else: a normal ops_agent run."""
    if payload.get("mode") == "next_steps":  # button click forwarded by the supervisor
        return {"result": next_steps_reply(payload.get("runbook_id", ""), payload.get("prompt", ""))}
    agent = build_agent()  # build the agent per request
    result = agent(payload.get("prompt", ""))  # triage question from the supervisor's ask_ops_agent tool
    return {"result": str(result)}  # same reply shape in both modes


if __name__ == "__main__":
    app.run()  # start a local server on port 8080 for testing