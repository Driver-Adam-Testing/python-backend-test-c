#!/bin/bash
set -euo pipefail
PS4='[${BASH_SOURCE}:${LINENO}] '
set -x

echo "deploying backend..."

#If there's a setEnv.sh script in the / directory, copy it and run it before starting
echo "Checking for setEnv script"
if [ -f "../build/setEnv.sh" ] ; then
    echo "Copy and run script setEnv.sh from deployment repo"
    cp "../build/setEnv.sh" .
    source setEnv.sh
elif [ -f "setEnv.sh" ] ; then
  source setEnv.sh
  echo "Using local setEnv.sh"
else
    echo "There is no script setEnv.sh"
fi

#Push backend contianers
aws ecr get-login-password --region $AWS_REGION | docker login --username AWS --password-stdin $AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com

#TODO convert all of this push logic to a function
BACKEND_IMAGE_NAME=python-backend
BACKEND_TAG=latest
BACKEND_REPO_URI=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$BACKEND_IMAGE_NAME:$BACKEND_TAG

HATCHET_WORKER_IMAGE_NAME=hatchet-worker
HATCHET_WORKER_TAG=latest
HATCHET_WORKER_REPO_URI=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$HATCHET_WORKER_IMAGE_NAME:$HATCHET_WORKER_TAG

SCIM_SERVER_IMAGE_NAME=scim-server
SCIM_SERVER_TAG=latest
SCIM_SERVER_REPO_URI=$AWS_ACCOUNT.dkr.ecr.$AWS_REGION.amazonaws.com/$SCIM_SERVER_IMAGE_NAME:$SCIM_SERVER_TAG


DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --build-arg GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD) -t $BACKEND_IMAGE_NAME -t $BACKEND_REPO_URI .
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --build-arg GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD) -t $HATCHET_WORKER_IMAGE_NAME -t $HATCHET_WORKER_REPO_URI  -f content_services/Dockerfile .
DOCKER_DEFAULT_PLATFORM=linux/amd64 docker build --build-arg GIT_COMMIT=$(git rev-parse HEAD) --build-arg GIT_BRANCH=$(git rev-parse --abbrev-ref HEAD) -t $SCIM_SERVER_IMAGE_NAME -t $SCIM_SERVER_REPO_URI -f scim/Dockerfile .

echo $BACKEND_REPO_URI
echo $HATCHET_WORKER_REPO_URI
echo $SCIM_SERVER_REPO_URI

BACKEND_LOCAL_DIGEST=$(docker image inspect $BACKEND_REPO_URI --format '{{json .Id}}' | grep -o 'sha256:[0-9a-f]\{64\}' )
HATCHET_WORKER_LOCAL_DIGEST=$(docker image inspect $HATCHET_WORKER_REPO_URI --format '{{json .Id}}' | grep -o 'sha256:[0-9a-f]\{64\}')

BACKEND_REMOTE_DIGEST=$(aws ecr describe-images \
  --repository-name "$BACKEND_IMAGE_NAME" \
  --image-ids imageTag="$BACKEND_TAG" \
  --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' \
  --output text 2>/dev/null || echo "NONE")

HATCHET_WORKER_REMOTE_DIGEST=$(aws ecr describe-images \
  --repository-name "$HATCHET_WORKER_IMAGE_NAME" \
  --image-ids imageTag="$HATCHET_WORKER_TAG" \
  --region "$AWS_REGION" \
  --query 'imageDetails[0].imageDigest' \
  --output text 2>/dev/null || echo "NONE")

if [ "$BACKEND_LOCAL_DIGEST" != "$BACKEND_REMOTE_DIGEST" ]; then
  echo "Backend Image has changed. Pushing new image..."
  docker push "$BACKEND_REPO_URI"
  export BACKEND_PUSHED=true
else
  echo "Image is up-to-date. No push needed."
  export BACKEND_PUSHED=false
fi

if [ "$HATCHET_WORKER_LOCAL_DIGEST" != "$HATCHET_WORKER_REMOTE_DIGEST" ]; then
  echo "Hatchet worker Image has changed. Pushing new image..."
  docker push "$HATCHET_WORKER_REPO_URI"
  export HATCHET_WORKER_PUSHED=true
else
  echo "Image is up-to-date. No push needed."
  export HATCHET_WORKER_PUSHED=false
fi

docker push "$SCIM_SERVER_REPO_URI"

uv sync --frozen --no-install-workspace

# Copy workspace lockfile to each lambda for UV-based bundling
echo "Copying uv.lock to lambda directories..."
for lambda_dir in lambdas/*/; do
    cp uv.lock "$lambda_dir"
done

set +e
npx cdk deploy --require-approval never
status=$?
set -e
# TODO allow pass through during "streaming updates" Other CLIs (PID=77666) are currently reading from cdk.out. Invoke the CLI in sequence, or use '--output' to synth into different directories."
if [[ $status -eq 1 ]]; then
    echo "CDK exited with status ${status}"
    exit 1
fi

#FORCE DEPLOY SECTION

CLUSTER_NAME=$(aws ecs list-clusters --query "clusterArns[?contains(@, 'V2BaseInfrastructureStack-BaseInfrastructureCoreInfrastructureCluster')]" --output text)

SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-ApiBackendBackendApiService')]" --output text)

HATCHET_BASE_WORKER_SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-HatchetBaselineWorkerHatchetWorkerSvc')]" --output text)

HATCHET_HEAVY_WORKER_SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-HatchetHeavyWorkerHatchetWorkerSvc')]" --output text)

HATCHET_ANALYTICS_WORKER_SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-HatchetAnalyticsWorkerHatchetWorkerSvc')]" --output text)

SCIM_SERVER_SERVICE_NAME=$(aws ecs list-services --cluster $CLUSTER_NAME --query "serviceArns[?contains(@, 'DriverApiStack-SCIMServer')]" --output text)

if [ "$BACKEND_PUSHED" = "true" ]; then
    echo "Forcing backend redeploy..."

    aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $SERVICE_NAME --force-new-deployment > /dev/null
fi

if [ "$HATCHET_WORKER_PUSHED" = "true" ]; then
    echo "Forcing hatchet worker redeploy..."

    aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $HATCHET_BASE_WORKER_SERVICE_NAME --force-new-deployment > /dev/null
    aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $HATCHET_HEAVY_WORKER_SERVICE_NAME --force-new-deployment > /dev/null
    aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $HATCHET_ANALYTICS_WORKER_SERVICE_NAME --force-new-deployment > /dev/null
fi

echo "Forcing SCIM server redeploy..."
aws --no-cli-pager ecs update-service --cluster $CLUSTER_NAME --service $SCIM_SERVER_SERVICE_NAME --force-new-deployment > /dev/null
#TODO Also wait for hatchet worker service to stablize?

echo "Waiting up to 5 minutes for ECS service to stabilize..."
set +e
timeout 300 aws ecs wait services-stable --cluster $CLUSTER_NAME --services $SERVICE_NAME
status=$?
set -e

if [[ $status -eq 0 ]]; then
    echo "✅ Service became stable."

    # This doesn't work for single tenant deployments where these APIs
    # aren't reachable. It's also redundant since the ALB is already
    # checking this very endpoint - the service wouldn't become stable
    # if these weren't succeeding, so I'm not seeing
    # the value of having our own redundant "health checks". The later log checking
    # was also causing running deployments to fail for unknown reasons.
    # Disabling.
    #
    #
    # URL="https://api.${URL_PREFIX}.driverai.com/api/v1/healthcheck/"
    # TIMEOUT=120   # 2 minutes in seconds
    # INTERVAL=5    # seconds between retries
    # START=$(date +%s)

    # while true; do
    #     STATUS=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 "$URL")

    #     if [ "$STATUS" -eq 200 ]; then
    #         echo "✅ Healthcheck successful: $URL returned 200"
    #         curl $URL
    #         exit 0
    #     fi

    #     NOW=$(date +%s)
    #     ELAPSED=$((NOW - START))

    #     if [ "$ELAPSED" -ge "$TIMEOUT" ]; then
    #         echo "❌ Healthcheck failed: $URL did not return 200 within $TIMEOUT seconds"
    #     fi

    #     sleep "$INTERVAL"
    # done
elif [[ $status -eq 124 ]]; then
  echo "⏰ Timed out waiting for service to become stable."
  exit 1
fi

# Debug print (safe)
# echo "Found tasks..."
# for i in "${!TASKS[@]}"; do
#   printf 'TASK[%d]=%s\n' "$i" "${TASKS[$i]}"
# done

# set -euo pipefail

# # Minutes to go back (env override: SINCE_MIN=10)
# SINCE_MIN="${SINCE_MIN:-2}"
# EXIT_ON_MATCH_CODE="${EXIT_ON_MATCH_CODE:-2}"
# START_MS=$(( ( $(date +%s) - SINCE_MIN*60 ) * 1000 ))
# MATCH_JSON='["error","fatal"]'
# MATCH_CASE_INSENSITIVE=1

# # Build patterns JSON array
# if [[ -n "${MATCH_JSON:-}" ]]; then
#   PATS_JSON="$MATCH_JSON"
# elif [[ -n "${MATCH:-}" ]]; then
#   # Convert CSV -> JSON array (trim spaces, drop empties)
#   PATS_JSON="$(jq -Rn --arg s "$MATCH" '($s|split(",")|map(gsub("^\\s+|\\s+$";""))|map(select(length>0)))')"
# else
#   echo "❌ Waiter failed with exit code ${status}."
#   exit 1
# fi

# Disabling. See comment above. Was breaking running deployments
# echo "checking server logs..."

# # 3) On timeout/failure: figure out which tasks are involved (RUNNING + STOPPED)
# TASKS=()
# while IFS= read -r line; do
#   [[ -n "$line" ]] && TASKS+=("$line")
# done < <(aws ecs list-tasks \
#   --cluster $CLUSTER_NAME \
#   --service-name $SERVICE_NAME \
#   --region $AWS_REGION \
#   --query 'taskArns' --output json | jq -r '.[]')

# # If no tasks returned at all, also check STOPPED (sometimes nothing is RUNNING yet)
# if [[ ${#TASKS[@]} -eq 0 || -z "${TASKS[0]:-}" ]]; then
#   TASKS=()
#   while IFS= read -r line; do
#     [[ -n "$line" ]] && TASKS+=("$line")
#   done < <(aws ecs list-tasks \
#     --cluster $CLUSTER_NAME \
#     --service-name $SERVICE_NAME \
#     --desired-status STOPPED \
#     --region $AWS_REGION \
#     --query 'taskArns' --output json | jq -r '.[]')
# fi

# if [[ ${#TASKS[@]} -eq 0 || -z "${TASKS[0]:-}" ]]; then
#   echo "Error: No tasks found for service ${SERVICE_NAME} in cluster ${CLUSTER_NAME}."
#   exit 1
# fi

# # Debug print (safe)
# echo "Found tasks..."
# for i in "${!TASKS[@]}"; do
#   printf 'TASK[%d]=%s\n' "$i" "${TASKS[$i]}"
# done

# set -euo pipefail

# # Minutes to go back (env override: SINCE_MIN=10)
# SINCE_MIN="${SINCE_MIN:-2}"
# EXIT_ON_MATCH_CODE="${EXIT_ON_MATCH_CODE:-2}"
# START_MS=$(( ( $(date +%s) - SINCE_MIN*60 ) * 1000 ))
# MATCH_JSON='["error","fatal"]'
# MATCH_CASE_INSENSITIVE=1

# # Build patterns JSON array
# if [[ -n "${MATCH_JSON:-}" ]]; then
#   PATS_JSON="$MATCH_JSON"
# elif [[ -n "${MATCH:-}" ]]; then
#   # Convert CSV -> JSON array (trim spaces, drop empties)
#   PATS_JSON="$(jq -Rn --arg s "$MATCH" '($s|split(",")|map(gsub("^\\s+|\\s+$";""))|map(select(length>0)))')"
# else
#   PATS_JSON="[]"
# fi

# # Expect a bash array named TASK with ECS task ARNs
# : "${TASKS[@]?Define a bash array TASK with your ECS task ARNs, e.g., TASKS[0]=arn:... }"

# for ARN in "${TASKS[@]}"; do
#   REGION="$(awk -F: '{print $4}' <<<"$ARN")"
#   CLUSTER="$(awk -F'/' '{print $(NF-1)}' <<<"$ARN")"
#   TASK_ID="$(awk -F'/' '{print $NF}' <<<"$ARN")"

#   TD_ARN="$(aws ecs describe-tasks \
#               --region "$REGION" \
#               --cluster "$CLUSTER" \
#               --tasks "$ARN" \
#               --query 'tasks[0].taskDefinitionArn' \
#               --output text)"

#   [[ -z "$TD_ARN" || "$TD_ARN" == "None" ]] && { echo "No taskDefinitionArn for $ARN"; continue; }

#   aws ecs describe-task-definition --region "$REGION" --task-definition "$TD_ARN" --output json \
#   | jq -r --arg DEFREG "$REGION" '
#       .taskDefinition.containerDefinitions[]
#       | select(.logConfiguration.logDriver=="awslogs")
#       | . as $c
#       | ($c.logConfiguration.options["awslogs-group"] // empty) as $group
#       | ($c.logConfiguration.options["awslogs-stream-prefix"] // empty) as $prefix
#       | ($c.name // empty) as $name
#       | ($c.logConfiguration.options["awslogs-region"] // $DEFREG) as $log_region
#       | select($group and $prefix and $name)
#       | [$group, $prefix, $name, $log_region]
#       | @tsv
#     ' \
#   | while IFS=$'\t' read -r GROUP PREFIX CNAME LOGREG; do
#       STREAM="$PREFIX/$CNAME/$TASK_ID"
#       echo "=== $REGION | cluster: $CLUSTER | $TASK_ID/$CNAME ==="
#       echo "Log group: $GROUP"
#       echo "Stream:    $STREAM"
#       echo

#       TOKEN=""
#       while :; do
#         if [[ -n "${TOKEN}" ]]; then
#           NEXT_TOKEN_ARG="--next-token ${TOKEN}"
#         else
#           NEXT_TOKEN_ARG=""
#         fi
#         RESP="$(aws logs filter-log-events \
#                   --region "$LOGREG" \
#                   --log-group-name "$GROUP" \
#                   --log-stream-names "$STREAM" \
#                   --start-time "$START_MS" \
#                   --interleaved \
#                   --limit 10000 \
#                   --output json ${NEXT_TOKEN_ARG})"

#         # Print logs (interleaved, timestamped)
#         jq -r --arg LABEL "$TASK_ID/$CNAME" '
#           .events[]
#           | "[\((.timestamp/1000)|strftime("%Y-%m-%d %H:%M:%SZ"))] \($LABEL) | \(.message|rtrimstr("\n"))"
#         ' <<<"$RESP"

#         # If patterns were provided, check for a match in this page and exit immediately if found
#         if [[ "$PATS_JSON" != "[]" ]]; then
#           if [[ "${MATCH_CASE_INSENSITIVE:-0}" == "1" ]]; then
#             jq -e --argjson pats "$PATS_JSON" '
#               ($pats | map(ascii_downcase)) as $lpats
#               | any(.events[]?; (.message // "") as $m
#                         | ($m | ascii_downcase) as $mm
#                         | any($lpats[]; $mm | contains(.)))
#             ' <<<"$RESP" >/dev/null && { echo ">>> Container likely did not start. Logs printed above. Error found in log $TASK_ID/$CNAME — exiting $EXIT_ON_MATCH_CODE"; exit "$EXIT_ON_MATCH_CODE"; }
#           else
#             jq -e --argjson pats "$PATS_JSON" '
#               any(.events[]?; (.message // "") as $m
#                         | any($pats[]?; $m | contains(.)))
#             ' <<<"$RESP" >/dev/null && { echo ">>> Container likely did not start. Logs printed above. Error found in log $TASK_ID/$CNAME — exiting $EXIT_ON_MATCH_CODE"; exit "$EXIT_ON_MATCH_CODE"; }
#           fi
#         fi

#         TOKEN="$(jq -r '.nextToken // empty' <<<"$RESP")"
#         [[ -z "$TOKEN" ]] && break
#       done

#       echo
#     done
# done
