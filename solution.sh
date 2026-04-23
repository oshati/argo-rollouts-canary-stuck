#!/bin/bash
set -e
export KUBECONFIG=/home/ubuntu/.kube/config

###############################################
# STEP 1: Neutralize all 4 saboteurs
# Must happen first — any fix applied before the saboteurs are dead will
# be reverted within 30-60 seconds.
###############################################
echo "[solution] Step 1: Neutralizing saboteurs..."

# Saboteur A: MutatingWebhookConfiguration in kube-system
kubectl delete mutatingwebhookconfiguration platform-config-sync 2>/dev/null || true
kubectl delete deployment platform-config-sync -n kube-system 2>/dev/null || true
kubectl delete service platform-config-sync -n kube-system 2>/dev/null || true
kubectl delete configmap platform-config-sync-rules -n kube-system 2>/dev/null || true

# Saboteur B: DaemonSet enforcer in monitoring
kubectl delete daemonset platform-health-monitor -n monitoring 2>/dev/null || true

# Saboteur C: CronJob drift-reconciler in argocd
kubectl delete cronjob platform-drift-reconciler -n argocd 2>/dev/null || true
kubectl delete job -n argocd -l app=platform-drift-reconciler --wait=false 2>/dev/null || true

# Saboteur D: ArgoCD Application selfHealing from Gitea
# Simplest oracle fix: delete the Application. Agents could also disable selfHeal.
kubectl delete application bleater-platform-rollouts -n argocd 2>/dev/null || true

# Decoy benign CronJob — suspend (shouldn't touch anything, but tidy)
kubectl patch cronjob rollout-slo-compliance-checker -n bleater -p '{"spec":{"suspend":true}}' 2>/dev/null || true

echo "[solution] Saboteurs neutralized."

###############################################
# STEP 2: Fix AnalysisTemplate (semantic-correct)
###############################################
echo "[solution] Step 2: Fixing AnalysisTemplate..."
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
    failureLimit: 1
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

###############################################
# STEP 3: Fix canary Service targetPort
# Discover the actual container port from the Rollout spec — don't
# hardcode, since the bleater image may expose a different port.
###############################################
echo "[solution] Step 3: Fixing canary service targetPort..."
LIKE_PORT=$(kubectl get rollout bleater-like-service -n bleater \
  -o jsonpath='{.spec.template.spec.containers[0].ports[0].containerPort}' 2>/dev/null)
LIKE_PORT=${LIKE_PORT:-8006}
echo "[solution] Detected container port: ${LIKE_PORT}"
kubectl patch service bleater-like-service-canary -n bleater --type json -p "[
  {\"op\": \"replace\", \"path\": \"/spec/ports/0/targetPort\", \"value\": ${LIKE_PORT}}
]" 2>/dev/null || true

###############################################
# STEP 4: Remove NetworkPolicy blocking canary traffic
###############################################
echo "[solution] Step 4: Removing NetworkPolicy..."
kubectl delete networkpolicy like-service-canary-isolation -n bleater 2>/dev/null || true

###############################################
# STEP 5: Fix cascading pod failures
###############################################
echo "[solution] Step 5: Fixing pod failures..."
# Missing ConfigMap
kubectl create configmap like-service-runtime-config -n bleater \
  --from-literal=SERVICE_NAME=like-service \
  --from-literal=LOG_LEVEL=info \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

# Remove broken init container + broken liveness probe
kubectl patch rollout bleater-like-service -n bleater --type json -p '[
  {"op": "remove", "path": "/spec/template/spec/initContainers"},
  {"op": "remove", "path": "/spec/template/spec/containers/0/livenessProbe"}
]' 2>/dev/null || true

###############################################
# STEP 6: Fix Rollout timeout configuration
###############################################
echo "[solution] Step 6: Adding timeout protection to Rollout..."
kubectl patch rollout bleater-like-service -n bleater --type merge -p '
{
  "spec": {
    "progressDeadlineSeconds": 1200,
    "progressDeadlineAbort": true,
    "strategy": {
      "canary": {
        "abortScaleDownDelaySeconds": 30
      }
    }
  }
}'

# Replace indefinite pause (step 1) with timed pause
kubectl patch rollout bleater-like-service -n bleater --type json -p '[
  {"op": "replace", "path": "/spec/strategy/canary/steps/1", "value": {"pause": {"duration": "60s"}}}
]' 2>/dev/null || true

###############################################
# STEP 7: Clean up all stale AnalysisRuns
###############################################
echo "[solution] Step 7: Cleaning up stale AnalysisRuns..."
kubectl delete analysisrun --all -n bleater 2>/dev/null || true

###############################################
# STEP 8: Abort and retry so the rollout picks up fixed config
###############################################
echo "[solution] Step 8: Abort + retry rollout..."
kubectl argo rollouts abort bleater-like-service -n bleater 2>/dev/null || true
sleep 10
kubectl argo rollouts retry rollout bleater-like-service -n bleater 2>/dev/null || true
sleep 5

###############################################
# STEP 9: Promote through paused steps
###############################################
echo "[solution] Step 9: Promoting through paused steps..."
for i in $(seq 1 15); do
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
