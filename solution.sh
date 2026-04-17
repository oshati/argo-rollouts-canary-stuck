#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

echo "[solution] Step 1: Stopping the enforcer CronJob..."

# The rollout-slo-compliance-checker deletes and recreates the AnalysisTemplate every minute
kubectl patch cronjob rollout-slo-compliance-checker -n bleater -p '{"spec":{"suspend":true}}' 2>/dev/null || true
# Kill running enforcer Jobs
for job in $(kubectl get jobs -n bleater -l job=slo-compliance -o name 2>/dev/null); do
  kubectl delete "$job" -n bleater --grace-period=0 2>/dev/null || true
done
# Delete the approved baseline so enforcer has nothing to restore
kubectl delete configmap analysis-template-approved -n bleater 2>/dev/null || true
echo "[solution] Enforcer stopped."

echo "[solution] Step 2: Fixing the AnalysisTemplate..."

kubectl apply -f - <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: bleater-like-service-error-rate
  namespace: bleater
  labels:
    app: bleater-like-service
    component: canary-analysis
spec:
  metrics:
  - name: error-rate
    interval: 60s
    count: 3
    inconclusiveLimit: 2
    successCondition: "len(result) == 0 || result[0] < 0.05"
    failureCondition: "len(result) > 0 && result[0] >= 0.10"
    provider:
      prometheus:
        address: http://prometheus.monitoring.svc.cluster.local:9090
        query: |
          (sum(rate(http_requests_total{service="bleater-like-service",status=~"5.."}[5m])) or on() vector(0))
          /
          (clamp_min(sum(rate(http_requests_total{service="bleater-like-service"}[5m])), 0.0001) or on() vector(1))
EOF

echo "[solution] AnalysisTemplate fixed."

echo "[solution] Step 3: Fixing canary service targetPort..."

# The canary service has targetPort: 8099 but the container listens on 8006
kubectl patch service bleater-like-service-canary -n bleater --type json -p '[
  {"op": "replace", "path": "/spec/ports/0/targetPort", "value": 8006}
]' 2>/dev/null || true

echo "[solution] Canary service targetPort fixed."

echo "[solution] Step 4: Fixing pod failures..."

# Create missing ConfigMap that envFrom references
kubectl create configmap like-service-runtime-config -n bleater \
  --from-literal=SERVICE_NAME=like-service \
  --from-literal=LOG_LEVEL=info \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

# Remove the broken initContainer that curls an unreachable IP
kubectl patch rollout bleater-like-service -n bleater --type json -p '[
  {"op": "remove", "path": "/spec/template/spec/initContainers"}
]' 2>/dev/null || true

echo "[solution] Pod failures fixed."

echo "[solution] Step 5: Patching Rollout (deadline, abort, timed pause)..."

kubectl patch rollout bleater-like-service -n bleater --type merge -p '
{
  "spec": {
    "progressDeadlineSeconds": 1200,
    "progressDeadlineAbort": true
  }
}'

# Replace indefinite pause with timed pause
kubectl patch rollout bleater-like-service -n bleater --type json -p '[
  {"op": "replace", "path": "/spec/strategy/canary/steps/1", "value": {"pause": {"duration": "60s"}}}
]' 2>/dev/null || true

echo "[solution] Rollout patched."

echo "[solution] Step 6: Cleaning up failed AnalysisRuns..."

kubectl delete analysisrun --all -n bleater 2>/dev/null || true

echo "[solution] Step 7: Aborting and retrying rollout..."

kubectl argo rollouts abort bleater-like-service -n bleater 2>/dev/null || true
sleep 10
kubectl argo rollouts retry rollout bleater-like-service -n bleater 2>/dev/null || true
sleep 5

echo "[solution] Step 8: Promoting through paused steps..."

for i in $(seq 1 10); do
  sleep 30
  STATUS=$(kubectl argo rollouts status bleater-like-service -n bleater --timeout 10s 2>/dev/null | head -1 || echo "unknown")
  echo "[solution] Rollout status: ${STATUS}"

  if echo "${STATUS}" | grep -qi "paused"; then
    echo "[solution] Promoting past pause..."
    kubectl argo rollouts promote bleater-like-service -n bleater 2>/dev/null || true
  elif echo "${STATUS}" | grep -qi "healthy"; then
    echo "[solution] Rollout is healthy — fully promoted."
    break
  fi
done

echo "[solution] ============================================"
echo "[solution] Solution complete."
echo "[solution] ============================================"
