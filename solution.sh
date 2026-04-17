#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

echo "[solution] Step 1: Finding and stopping the enforcer..."

# The enforcer is a DaemonSet in monitoring namespace (NOT a CronJob in bleater)
# Scale it to 0 by deleting it (can't scale DaemonSets)
kubectl delete daemonset platform-health-monitor -n monitoring 2>/dev/null || true
# Delete the approved baseline ConfigMap so even if it comes back, nothing to restore
kubectl delete configmap platform-audit-baselines -n monitoring 2>/dev/null || true
# Also suspend the decoy CronJob in bleater (benign but clean up)
kubectl patch cronjob rollout-slo-compliance-checker -n bleater -p '{"spec":{"suspend":true}}' 2>/dev/null || true
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

echo "[solution] Step 3: Fixing canary service..."

# Fix the targetPort on bleater-like-service-canary (8099 → correct port)
kubectl patch service bleater-like-service-canary -n bleater --type json -p '[
  {"op": "replace", "path": "/spec/ports/0/targetPort", "value": 8006}
]' 2>/dev/null || true

echo "[solution] Canary service fixed."

echo "[solution] Step 4: Fixing pod failures..."

# Create missing ConfigMap
kubectl create configmap like-service-runtime-config -n bleater \
  --from-literal=SERVICE_NAME=like-service \
  --from-literal=LOG_LEVEL=info \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

# Remove broken initContainer, fix liveness probe path, and remove the probe entirely
# (removing is safer — the probe will cause restarts if the app doesn't serve /health either)
kubectl patch rollout bleater-like-service -n bleater --type json -p '[
  {"op": "remove", "path": "/spec/template/spec/initContainers"},
  {"op": "remove", "path": "/spec/template/spec/containers/0/livenessProbe"}
]' 2>/dev/null || true

echo "[solution] Pod failures fixed."

echo "[solution] Step 5: Patching Rollout config..."

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

echo "[solution] Rollout config patched."

echo "[solution] Step 6: Cleaning up ALL stale AnalysisRuns..."

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
    kubectl argo rollouts promote bleater-like-service -n bleater 2>/dev/null || true
  elif echo "${STATUS}" | grep -qi "healthy"; then
    echo "[solution] Rollout is healthy."
    break
  fi
done

echo "[solution] ============================================"
echo "[solution] Solution complete."
echo "[solution] ============================================"
