#!/usr/bin/env bash
# Start, stop or check the one sandbox resource that bills while idle: the ops-bridge EC2.
set -euo pipefail  # stop on any error, unset variable or failed pipe

PROFILE="${AWS_PROFILE:-aws-bedrock-learning}"  # CLI profile; override by exporting AWS_PROFILE
REGION="ap-southeast-2"
NAME_TAG="ops-bridge"       # EC2 Name tag from Step 16c
API_NAME="ops-bridge-api"   # HTTP API from Step 17d

aws_() { aws --profile "$PROFILE" --region "$REGION" "$@"; }  # every AWS call uses the same profile and region

INSTANCE_ID=$(aws_ ec2 describe-instances \
  --filters "Name=tag:Name,Values=$NAME_TAG" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)  # find the instance by tag, skip terminated ones
[[ "$INSTANCE_ID" == "None" ]] && { echo "No $NAME_TAG instance found" >&2; exit 1; }

API=$(aws_ apigatewayv2 get-apis --query "Items[?Name=='$API_NAME'].ApiEndpoint | [0]" --output text)  # public HTTPS URL

health() { curl -s --max-time 5 "$API/health" || echo "unreachable"; }  # bridge check through API Gateway

case "${1:-status}" in
  start)
    aws_ ec2 start-instances --instance-ids "$INSTANCE_ID" >/dev/null
    echo "Starting $INSTANCE_ID ..."
    aws_ ec2 wait instance-running --instance-ids "$INSTANCE_ID"  # blocks until EC2 reports running
    for _ in $(seq 1 30); do  # then up to 150 s for Docker and the container to come up
      [[ "$(health)" == *'"ok"'* ]] && { echo "Bridge healthy: $API"; exit 0; }
      sleep 5
    done
    echo "Instance running but bridge not healthy; on the instance run: sudo docker ps" >&2; exit 1 ;;
  stop)
    aws_ ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null
    echo "Stopping $INSTANCE_ID ..."
    aws_ ec2 wait instance-stopped --instance-ids "$INSTANCE_ID"  # blocks until fully stopped
    echo "Stopped. Slack alerts posted now will not be triaged." ;;
  status)
    STATE=$(aws_ ec2 describe-instances --instance-ids "$INSTANCE_ID" --query 'Reservations[0].Instances[0].State.Name' --output text)
    echo "EC2 $INSTANCE_ID: $STATE"
    echo "Bridge /health: $(health)" ;;
  *)
    echo "usage: scripts/sandbox.sh start|stop|status" >&2; exit 2 ;;
esac