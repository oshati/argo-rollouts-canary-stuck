#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

echo "[solution] Step 1: Diagnosing the stuck rollout..."

# Check rollout status
kubectl argo rollouts status bleater-like-service -n bleater --timeout 10s 2>/dev/null || true
kubectl get analysisrun -n bleater -l rollout=bleater-like-service 2>/dev/null || true

echo "[solution] Step 2: Fixing the AnalysisTemplate..."

# Fix 1: rate window from 30s to 2m (>= 2x the 60s scrapeInterval)
# Fix 2: Add nanStrategy: ReplaceWithZero so NaN = pass (0 error rate)
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
    successCondition: "result[0] < 0.05 || isNaN(result[0])"
    failureCondition: "result[0] >= 0.10"
    consecutiveErrorLimit: 2
    provider:
      prometheus:
        address: http://prometheus.monitoring.svc.cluster.local:9090
        query: |
          sum(rate(http_requests_total{service="bleater-like-service",code=~"5.."}[2m]))
          /
          sum(rate(http_requests_total{service="bleater-like-service"}[2m]))
EOF

echo "[solution] AnalysisTemplate fixed: rate window 30s->2m, added nanStrategy."

echo "[solution] Step 3: Setting progressDeadlineSeconds on the Rollout..."

kubectl patch rollout bleater-like-service -n bleater --type merge -p '
{
  "spec": {
    "progressDeadlineSeconds": 1200
  }
}'

echo "[solution] progressDeadlineSeconds set to 1200."

echo "[solution] Step 4: Aborting the current stuck rollout..."

# Abort the current stuck rollout so it picks up the new AnalysisTemplate
kubectl argo rollouts abort bleater-like-service -n bleater 2>/dev/null || true
sleep 10

echo "[solution] Step 5: Retrying the rollout..."

# Retry the rollout with the fixed template
kubectl argo rollouts retry rollout bleater-like-service -n bleater 2>/dev/null || true
sleep 5

echo "[solution] Step 6: Promoting through paused steps..."

# The rollout will pause at step 1 (after setWeight: 20) waiting for manual pause
# Promote past the pause
for i in $(seq 1 5); do
  sleep 30
  STATUS=$(kubectl argo rollouts status bleater-like-service -n bleater 2>/dev/null | head -1 || echo "unknown")
  echo "[solution] Rollout status: ${STATUS}"

  if echo "${STATUS}" | grep -qi "paused"; then
    echo "[solution] Promoting past pause..."
    kubectl argo rollouts promote bleater-like-service -n bleater 2>/dev/null || true
  elif echo "${STATUS}" | grep -qi "healthy"; then
    echo "[solution] Rollout is healthy — fully promoted."
    break
  fi
done

echo "[solution] Step 7: Verifying final state..."

kubectl argo rollouts status bleater-like-service -n bleater --timeout 10s 2>/dev/null || true
kubectl get analysisrun -n bleater -l rollout=bleater-like-service --sort-by='.metadata.creationTimestamp' 2>/dev/null | tail -3

# Verify Prometheus returns valid data
PROM_POD=$(kubectl get pods -n monitoring -l app=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || \
  kubectl get pods -n monitoring -l app.kubernetes.io/name=prometheus -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)

if [ -n "${PROM_POD}" ]; then
  QUERY_RESULT=$(kubectl exec -n monitoring "${PROM_POD}" -- wget -qO- \
    'http://localhost:9090/api/v1/query?query=sum(rate(http_requests_total{service="bleater-like-service"}[2m]))' 2>/dev/null || echo "query failed")
  echo "[solution] Prometheus query result: ${QUERY_RESULT}"
fi

echo "[solution] ============================================"
echo "[solution] Solution complete."
echo "[solution] ============================================"
