from bedrock_agentcore.runtime import BedrockAgentCoreApp  # AgentCore's wrapper: turns our code into an HTTP service

from src.agents.ops_agent import build_agent  # our agent factory, now in the src/agents package
app = BedrockAgentCoreApp()  # creates the web app that AgentCore will call


@app.entrypoint  # registers this function as the handler for every invocation
def invoke(payload):
    prompt = payload.get("prompt", "")  # callers send JSON like {"prompt": "..."}
    agent = build_agent()  # fresh agent per request, so no chat history leaks between calls
    result = agent(prompt)  # run the model-and-tools loop, guardrail included
    return {"result": str(result)}  # return JSON-serialisable output


if __name__ == "__main__":
    app.run()  # start a local server on port 8080 for testing