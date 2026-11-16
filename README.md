# Ops Agent: Slack-driven alert triage on Amazon Bedrock AgentCore

Alerts posted in a Slack channel are triaged by an AI supervisor agent. It rates each alert, asks a specialist
agent for evidence (recent deployments, related alerts, matching runbook), and replies in the alert's thread.

Region: set by `AWS_REGION` in `.env`. Every command below uses `[YOUR-AWS-REGION]` and `[YOUR-AWS-PROFILE]` as
placeholders; replace them with your own values.

## Architecture

```
Slack channel (alert posted)
   |  Events API (HTTPS, signed)
   v
API Gateway HTTP API  --(VPC link + Cloud Map, private)-->  EC2: FastAPI bridge container (port 8000)
                                                                |  1. verify Slack signature, reply 200 in < 3 s
                                                                |  2. background task: invoke_agent_runtime
                                                                v
                                              AgentCore Runtime: ops_supervisor_agent
                                                                |  ask_ops_agent tool (only when evidence is needed)
                                                                v
                                              AgentCore Runtime: ops_agent
                                                  tools: deployments, alerts, runbook search
                                                                |
                                                                v
                                    Amazon Bedrock: Nova Pro model, Guardrail, Knowledge Base (runbook PDFs)

The bridge posts the supervisor's answer back into the alert's Slack thread.
```

Secrets (Slack bot token and signing secret) live only in AWS Secrets Manager. `.env` holds non-secret settings.
No AWS access keys are stored anywhere: locally the AWS CLI profile is used, in the cloud each component uses its
own IAM role.

## Repository layout

| Path | Purpose |
|---|---|
| `app.py` | AgentCore entrypoint for `ops_agent` |
| `supervisor_app.py` | AgentCore entrypoint for `ops_supervisor_agent` |
| `src/agents/ops_agent.py` | Specialist agent: deployments, alerts, runbook search |
| `src/agents/ops_supervisor_agent.py` | Supervisor agent: rates alerts, routes to specialists |
| `src/tools/ops_tools.py` | `ops_agent` tools (sample data + Knowledge Base search) |
| `src/tools/supervisor_tools.py` | `ask_ops_agent` tool (calls the deployed `ops_agent`) |
| `src/api/main.py` | FastAPI bridge: `/slack/events`, `/health` |
| `src/api/slack.py` | Slack helpers: secrets, signature check, event filter, thread reply |
| `config/settings.py` | Loads `.env` and the YAML files below |
| `config/*.yaml` | Agent prompts, model settings, timeouts, logging |
| `data/knowledge_base/runbooks/` | Runbook PDFs (RB-001 to RB-010) for the Knowledge Base |
| `Dockerfile.bridge` | Container image for the FastAPI bridge |
| `scripts/launch.sh` | Deploys one agent to AgentCore with all `.env` values except `AWS_PROFILE` |
| `scripts/sandbox.sh` | Starts, stops or checks the bridge EC2 |
| `scripts/send_test_event.py` | Sends Slack-signed test requests to the bridge |
| `scripts/iam/*.example.json` | IAM policy templates; copy to `*.json` (git-ignored) and fill in your values |
| `examples/sample_alerts.yaml` | 22 sample alerts grouped by expected runbook |

## Prerequisites

- An AWS account and an AWS CLI v2 profile with admin-level rights for setup (`aws configure --profile [YOUR-AWS-PROFILE]`).
- A region that offers Bedrock AgentCore Runtime, Bedrock Knowledge Bases with S3 Vectors, and the models below.
- Bedrock model access in that region for **Amazon Nova Pro** (`amazon.nova-pro-v1:0`) and **Amazon Titan Text Embeddings V2**.
- Python 3.12 or later.
- Docker (Docker Desktop or similar) running locally.
- A Slack workspace where you can create apps.

## 1. Local setup

```bash
git clone [REPO-URL] ops-agent && cd ops-agent
python3 -m venv .venv
source .venv/bin/activate          # repeat in every new terminal
pip install -r requirements.txt    # all packages are pinned here; never pip install packages directly
cp .env.example .env
```

If the repository contains `.bedrock_agentcore.yaml` or a `.bedrock_agentcore/` folder from someone else's
deployment, delete them. The toolkit writes your own versions in steps 3 and 4.

Optional: the AgentCore starter toolkit prints a deprecation notice on every command. To hide it:

```bash
export AGENTCORE_SUPPRESS_RECOMMENDATION=1
```

Fill `.env` as you go. Final contents:

```
AWS_PROFILE=[YOUR-AWS-PROFILE]
AWS_REGION=[YOUR-AWS-REGION]
MODEL_ID=amazon.nova-pro-v1:0
RUNBOOK_BUCKET=[RUNBOOK-BUCKET-NAME]
KNOWLEDGE_BASE_ID=[KNOWLEDGE-BASE-ID]
GUARDRAIL_ID=[GUARDRAIL-ID]
GUARDRAIL_VERSION=[GUARDRAIL-VERSION]
OPS_AGENT_RUNTIME_ARN=[OPS-AGENT-RUNTIME-ARN]
SLACK_SECRET_ID=ops-supervisor/slack
SLACK_ALERT_CHANNEL_ID=[SLACK-CHANNEL-ID]
SUPERVISOR_RUNTIME_ARN=[SUPERVISOR-RUNTIME-ARN]
```

Make sure the last line of `.env` ends with a newline.

## 2. Knowledge Base and Guardrail (AWS console)

**Runbook bucket**
1. S3, create a bucket `[RUNBOOK-BUCKET-NAME]` in `[YOUR-AWS-REGION]`.
2. Upload every PDF from `data/knowledge_base/runbooks/`.

**Knowledge Base**
1. Bedrock, Knowledge Bases, create a knowledge base with a vector store.
2. Data source: the S3 bucket above. Embeddings model: Titan Text Embeddings V2. Vector store: quick create with S3 Vectors.
3. After it is created, select the data source and click **Sync**.
4. Copy the Knowledge Base ID into `KNOWLEDGE_BASE_ID`.

**Guardrail**
1. Bedrock, Guardrails, create a guardrail.
2. Content filters (harmful categories): Medium. Prompt attacks: High, on input only.
3. Sensitive information (PII): block or mask the types you care about.
4. Create a version (for example `1`). Copy the guardrail ID into `GUARDRAIL_ID` and the version into `GUARDRAIL_VERSION`.
   Use a numbered version, not `DRAFT`.

Check that the agent works locally:

```bash
python -m src.agents.ops_agent
```

## 3. Deploy `ops_agent` to AgentCore

```bash
agentcore configure -e app.py -n ops_agent -r [YOUR-AWS-REGION]
```

Answer the prompts: execution role auto-create, ECR auto-create, dependency file `requirements.txt`,
no OAuth, no request header allowlist, no memory.

```bash
./scripts/launch.sh ops_agent
```

`launch.sh` passes every `.env` value except `AWS_PROFILE` to the runtime with `--env`. The container never
sees `.env` itself, and in AWS it uses its IAM role instead of a profile.

Find the runtime's execution role and ARN:

```bash
grep -n "agent_arn:\|execution_role:" .bedrock_agentcore.yaml
```

Grant the role access to the Knowledge Base and the guardrail. Copy the template, then replace every
`[PLACEHOLDER]` in the copy with your values:

```bash
cp scripts/iam/ops-agent-policy.example.json scripts/iam/ops-agent-policy.json
```

```bash
aws iam put-role-policy --role-name [OPS-AGENT-EXECUTION-ROLE-NAME] --policy-name ops-agent-kb-guardrail \
  --policy-document file://scripts/iam/ops-agent-policy.json --profile [YOUR-AWS-PROFILE]
```

Test, then copy the `ops_agent` runtime ARN into `OPS_AGENT_RUNTIME_ARN` in `.env`:

```bash
agentcore invoke '{"prompt": "Checkout payment errors are spiking. What changed recently and which runbook applies?"}' -a ops_agent
```

## 4. Deploy `ops_supervisor_agent` to AgentCore

`OPS_AGENT_RUNTIME_ARN` must be in `.env` before this step.

```bash
agentcore configure -e supervisor_app.py -n ops_supervisor_agent -r [YOUR-AWS-REGION]
./scripts/launch.sh ops_supervisor_agent
```

`configure` makes the new agent the toolkit's default. Always pass `-a <agent>` to `agentcore invoke`, and an
agent name to `launch.sh`.

Grant the supervisor's execution role (a different role from `ops_agent`'s) permission to call `ops_agent` and to
apply the guardrail. Copy the template and fill in its placeholders:

```bash
cp scripts/iam/ops-supervisor-policy.example.json scripts/iam/ops-supervisor-policy.json
```

```bash
aws iam put-role-policy --role-name [SUPERVISOR-EXECUTION-ROLE-NAME] --policy-name ops-supervisor-invoke-guardrail \
  --policy-document file://scripts/iam/ops-supervisor-policy.json --profile [YOUR-AWS-PROFILE]
```

Test, then copy the supervisor runtime ARN into `SUPERVISOR_RUNTIME_ARN` in `.env`:

```bash
agentcore invoke '{"prompt": "[FIRING] checkout: payment error rate 12% (threshold 2%) for 10 minutes."}' -a ops_supervisor_agent
```

## 5. Slack app

1. https://api.slack.com/apps, **Create New App**, **From scratch**. Name it, pick your workspace.
2. **OAuth & Permissions**, **Bot Token Scopes**: add `chat:write`, `channels:history`, `channels:read`.
3. **Install to Workspace**, **Allow**. Keep the **Bot User OAuth Token** (`xoxb-...`) for step 6.
   Whenever you change scopes, click **Reinstall to Workspace**.
4. **Basic Information**, **App Credentials**: keep the **Signing Secret** for step 6.
5. **Settings**, **Socket Mode**: leave it **off**. With Socket Mode on, Slack ignores the Request URL and no
   events reach the bridge.
6. In Slack, create a public alert channel and run `/invite @[YOUR-BOT-NAME]` in it.
7. Get the channel ID (channel details, bottom of the **About** tab, starts with `C`) and put it in `SLACK_ALERT_CHANNEL_ID`.

Do not set the Event Subscriptions Request URL yet. Slack verifies it immediately, so the API must be live first (step 10).

Treat the bot token and signing secret like passwords. Never put them in `.env`, code, the image, or chat.

## 6. Slack secrets in Secrets Manager (console)

1. Secrets Manager in `[YOUR-AWS-REGION]`, **Store a new secret**, **Other type of secret**.
2. Key/value pairs: `bot_token` = the `xoxb-...` token, `signing_secret` = the signing secret.
3. Encryption key: `aws/secretsmanager`. Secret name: `ops-supervisor/slack`. Rotation: off.

Check without printing the values:

```bash
aws secretsmanager get-secret-value --secret-id ops-supervisor/slack --query SecretString --output text \
  --region [YOUR-AWS-REGION] --profile [YOUR-AWS-PROFILE] \
  | python -c "import sys, json; d = json.loads(sys.stdin.read()); print(sorted(d), d['bot_token'].startswith('xoxb-'))"
```

## 7. Test the bridge locally

Terminal 1:

```bash
uvicorn src.api.main:app --port 8000
```

Terminal 2:

```bash
PYTHONPATH=. python scripts/send_test_event.py challenge   # expect 200 {"challenge": ...}
PYTHONPATH=. python scripts/send_test_event.py forged      # expect 401 {"error":"invalid signature"}
PYTHONPATH=. python scripts/send_test_event.py alert       # expect 200 {"ok":true}, then a triage reply in Slack
```

## 8. Build the image and push it to ECR

The image architecture must match the EC2 instance. These steps use an arm64 (Graviton) instance. On an
x86 machine, add `--platform linux/arm64` to `docker build`.

```bash
docker build -f Dockerfile.bridge -t ops-bridge:local .
```

Optional local run (mounts your AWS profile read-only; for local testing only):

```bash
docker run --rm -d --name ops-bridge -p 8000:8000 --env-file .env -v ~/.aws:/home/appuser/.aws:ro ops-bridge:local
```

Create a private ECR repository named `ops-bridge` (console: Elastic Container Registry, Create repository), then:

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --profile [YOUR-AWS-PROFILE])
REGISTRY=$ACCOUNT_ID.dkr.ecr.[YOUR-AWS-REGION].amazonaws.com
aws ecr get-login-password --region [YOUR-AWS-REGION] --profile [YOUR-AWS-PROFILE] \
  | docker login --username AWS --password-stdin $REGISTRY
docker tag ops-bridge:local $REGISTRY/ops-bridge:v1
docker push $REGISTRY/ops-bridge:v1
```

## 9. EC2 host for the bridge

**IAM role** (console: IAM, Roles, Create role, AWS service, EC2), named `ops-bridge-ec2-role`:
- Managed policies: `AmazonSSMManagedInstanceCore`, `AmazonEC2ContainerRegistryReadOnly`.
- Inline policy `ops-bridge-runtime-access`: copy the template, fill in its placeholders, then attach it:

```bash
cp scripts/iam/ops-bridge-ec2-policy.example.json scripts/iam/ops-bridge-ec2-policy.json
aws iam put-role-policy --role-name ops-bridge-ec2-role --policy-name ops-bridge-runtime-access \
  --policy-document file://scripts/iam/ops-bridge-ec2-policy.json --profile [YOUR-AWS-PROFILE]
```

**Instance** (console: EC2, Launch instances):
- Name `ops-bridge`. AMI: Amazon Linux 2023, 64-bit (Arm). Type: `t4g.small`. Key pair: none (Session Manager is used).
- Network: default VPC, public IP enabled. New security group `ops-bridge-sg` with **no inbound rules**
  (remove the default SSH rule). Step 10 adds the only inbound rule.
- Advanced details: IAM instance profile `ops-bridge-ec2-role`; Metadata version **V2 only**;
  Metadata response hop limit **2** (with 1, the container cannot get IAM credentials).
- User data:

```bash
#!/bin/bash
dnf install -y docker
systemctl enable --now docker
```

**Run the container** (EC2, Connect, Session Manager):

```bash
sudo dnf install -y nano && sudo mkdir -p /opt/ops-bridge && sudo nano /opt/ops-bridge/bridge.env
```

`/opt/ops-bridge/bridge.env` (no `AWS_PROFILE`, so the instance role is used):

```
AWS_REGION=[YOUR-AWS-REGION]
SLACK_SECRET_ID=ops-supervisor/slack
SLACK_ALERT_CHANNEL_ID=[SLACK-CHANNEL-ID]
SUPERVISOR_RUNTIME_ARN=[SUPERVISOR-RUNTIME-ARN]
```

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT_ID.dkr.ecr.[YOUR-AWS-REGION].amazonaws.com
aws ecr get-login-password --region [YOUR-AWS-REGION] | sudo docker login --username AWS --password-stdin $REGISTRY
sudo docker run -d --name ops-bridge --restart unless-stopped -p 8000:8000 \
  --env-file /opt/ops-bridge/bridge.env $REGISTRY/ops-bridge:v1
sudo docker logs ops-bridge 2>&1 | grep ops_bridge    # expect: Slack secrets loaded; bridge ready
```

## 10. Private HTTPS endpoint (API Gateway, VPC link, Cloud Map)

1. **Cloud Map**: create namespace `ops-bridge` (instance discovery: API calls), service `bridge`, and register a
   service instance: type **IP address**, ID `ops-bridge-ec2`, IPv4 = the instance's **private** IP, port `8000`.
2. **Security groups**: create `ops-bridge-vpclink-sg` (no inbound rules). In `ops-bridge-sg`, add inbound
   TCP `8000` from source `ops-bridge-vpclink-sg`.
3. **VPC link**: API Gateway, VPC links, create a **VPC link for HTTP APIs** in the default VPC, all subnets,
   security group `ops-bridge-vpclink-sg`. Wait for `Available`.
4. **HTTP API** `ops-bridge-api`: create it empty, add routes `POST /slack/events` and `GET /health`, then attach
   one **Private resource** integration to both: target service Cloud Map, namespace `ops-bridge`, service `bridge`,
   VPC link `ops-bridge-vpclink`. Keep the `$default` stage with auto-deploy.

Test:

```bash
API=$(aws apigatewayv2 get-apis --query 'Items[?Name==`ops-bridge-api`].ApiEndpoint' --output text \
  --region [YOUR-AWS-REGION] --profile [YOUR-AWS-PROFILE])
curl -s $API/health                                                   # {"status":"ok"}
BRIDGE_URL=$API/slack/events PYTHONPATH=. python scripts/send_test_event.py challenge
```

## 11. Connect Slack

Slack app, **Event Subscriptions**: enable events, Request URL `[API-ENDPOINT]/slack/events` (wait for
**Verified**), subscribe to the bot event `message.channels`, **Save Changes**, reinstall if asked.

Post an alert as a new top-level message in the alert channel. A triage reply appears in its thread within about
a minute. Replies inside threads, including the bot's own, are ignored. `examples/sample_alerts.yaml` has 22 alerts
grouped by the runbook each should map to.

## Daily operations

```bash
./scripts/sandbox.sh status    # EC2 state and bridge health through API Gateway
./scripts/sandbox.sh stop      # stop the EC2 when not testing (alerts posted while stopped are lost)
./scripts/sandbox.sh start     # start it again and wait until the bridge is healthy
```

The private IP survives stop and start, so Cloud Map and the VPC link keep working. After a start, check that the
Slack app's Event Subscriptions page still shows the Request URL as verified.

**Redeploy an agent** after changing its code, prompt or `.env` values:

```bash
./scripts/launch.sh ops_agent              # or ops_supervisor_agent
```

**Redeploy the bridge** after changing `src/api/` or `config/`: build, tag a new version (`v2`), push, then on
the EC2:

```bash
sudo docker pull $REGISTRY/ops-bridge:v2
sudo docker rm -f ops-bridge
sudo docker run -d --name ops-bridge --restart unless-stopped -p 8000:8000 \
  --env-file /opt/ops-bridge/bridge.env $REGISTRY/ops-bridge:v2
```

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Runtime returns 500, logs show `modelId` is `None` | Runtime deployed without `.env` values | Deploy with `./scripts/launch.sh <agent>`, not a bare `agentcore launch` |
| `ops_agent call failed: AccessDenied` | Supervisor role lacks `InvokeAgentRuntime` | Check the step 4 inline policy and its ARNs |
| Bridge fails at startup reading the secret | Role cannot read the secret, or hop limit is 1 | Check `ops-bridge-ec2-role` and set metadata hop limit to 2 |
| Slack messages produce no `POST /slack/events` in the logs | Socket Mode is on, or Event Subscriptions disabled | Turn Socket Mode off and re-save the Request URL |
| `missing_scope` from Slack | Scope added without reinstalling | **Reinstall to Workspace** |
| `401 invalid signature` for real Slack events | Wrong signing secret in Secrets Manager | Update the secret, restart the container |
| `Cannot connect to the Docker daemon` | Docker Desktop not running | Start Docker Desktop |
| Reply contains `<thinking>` text | Model reasoning leaked | Both `supervisor_app.py` and the bridge strip it; redeploy if an old version is running |

## Cost and cleanup

Billed while idle: the EC2 instance (about $15 to $20 a month running, under $1 stopped), the Secrets Manager
secret (about $0.40 a month), Cloud Map (about $0.10 a month), and storage in ECR and S3 Vectors (cents).
AgentCore runtimes, API Gateway, the VPC link and Bedrock are billed per use. Check current prices for your region.

Delete in this order when finished: Slack Event Subscriptions; API Gateway API and VPC link; Cloud Map
service and namespace; EC2 instance, its security groups and `ops-bridge-ec2-role`; ECR repository `ops-bridge`;
the secret; both runtimes (`agentcore destroy -a <agent>`), their execution roles, inline policies and log
groups; the Knowledge Base and its S3 Vectors store; the runbook bucket; the guardrail; finally the Slack app.

## POC limitations

- The bridge hands alerts to the supervisor with FastAPI `BackgroundTasks`, inside the container. A restart
  mid-triage loses that alert. Production should put alerts on a queue (for example SQS) and process them in a worker.
- Slack retries are skipped only when Slack reports a timeout. Production should deduplicate by `event_id`
  in shared storage (for example DynamoDB).
- A single EC2 instance has no redundancy. In production, run the same image as several pods behind a Kubernetes
  Ingress with TLS, and give pods their own IAM role (IRSA or EKS Pod Identity).
- `ops_agent`'s deployment and alert tools return sample data. Connect them to your real CI/CD and monitoring.
- The AgentCore starter toolkit used here is deprecated in favour of the AgentCore CLI (`@aws/agentcore`).