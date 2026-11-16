#!/usr/bin/env bash
set -euo pipefail  # stop on any error, undefined variable or failed pipe

cd "$(dirname "$0")/.."  # run from the project root, wherever the script is called from
AGENT="${1:?usage: scripts/launch.sh <agent-name>}"  # required first argument: ops_agent or ops_supervisor_agent; stops with this message if missing
set -a; source .env; set +a  # export everything in .env, so the agentcore CLI sees AWS_PROFILE

args=()  # the --env options we will pass to launch
while IFS= read -r line || [[ -n "$line" ]]; do  # read .env one line at a time; also handle a last line with no trailing newline
  [[ -z "$line" || "$line" == \#* || "$line" == AWS_PROFILE=* ]] && continue  # skip blank lines, comments and AWS_PROFILE: the cloud uses the IAM role
  args+=(--env "$line")  # add one --env KEY=VALUE option
done < .env

if [[ "${DRY_RUN:-0}" == "1" ]]; then
  echo agentcore launch --agent "$AGENT" "${args[@]}"  # print the command only
else
  agentcore launch --agent "$AGENT" "${args[@]}"  # deploy the named agent with every .env value except AWS_PROFILE
fi