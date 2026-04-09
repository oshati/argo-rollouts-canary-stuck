#!/bin/bash
set -eo pipefail
exec 1> >(stdbuf -oL cat) 2>&1

###############################################
# ENVIRONMENT SETUP
###############################################
export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

echo "[setup] Waiting for k3s node to be Ready..."
until kubectl get nodes 2>/dev/null | grep -q " Ready"; do sleep 2; done
echo "[setup] k3s is Ready."

mkdir -p /home/ubuntu/.kube
cp /etc/rancher/k3s/k3s.yaml /home/ubuntu/.kube/config
chown -R ubuntu:ubuntu /home/ubuntu/.kube
chmod 600 /home/ubuntu/.kube/config

###############################################
# IMPORT CONTAINER IMAGES
###############################################
echo "[setup] Importing container images..."
CTR="ctr --address /run/k3s/containerd/containerd.sock -n k8s.io"
until [ -S /run/k3s/containerd/containerd.sock ]; do sleep 2; done
sleep 5

for img in /var/lib/rancher/k3s/agent/images/*.tar; do
  imgname=$(basename "$img")
  echo "[setup] Importing ${imgname}..."
  for attempt in $(seq 1 5); do
    if $CTR images import "$img" 2>/dev/null; then
      echo "[setup] ${imgname} imported."
      break
    fi
    sleep 10
  done
done

###############################################
# WAIT FOR BLEATER ECOSYSTEM
###############################################
echo "[setup] Waiting for bleater namespace and like-service..."
until kubectl get ns bleater >/dev/null 2>&1; do sleep 3; done

# Bleater takes time to fully bootstrap — wait for the namespace to have pods
for i in $(seq 1 120); do
  if kubectl get deployment like-service -n bleater >/dev/null 2>&1; then
    echo "[setup] like-service deployment found."
    break
  fi
  echo "[setup] Waiting for like-service deployment... (attempt $i)"
  sleep 10
done

kubectl rollout status deployment/like-service -n bleater --timeout=600s 2>/dev/null || true
kubectl wait --for=condition=ready pod -l app=like-service -n bleater --timeout=300s 2>/dev/null || true

# Wait for Prometheus
echo "[setup] Waiting for Prometheus..."
kubectl rollout status deployment/prometheus -n monitoring --timeout=300s 2>/dev/null || true

###############################################
# INSTALL ARGO ROLLOUTS
###############################################
echo "[setup] Installing Argo Rollouts..."

kubectl create namespace argo-rollouts 2>/dev/null || true
kubectl apply -n argo-rollouts -f /opt/argo-rollouts/install.yaml 2>/dev/null || true

# Wait for controller — but it may fail to pull if no internet
# Use the pre-loaded image
kubectl set image deployment/argo-rollouts -n argo-rollouts \
  argo-rollouts=quay.io/argoproj/argo-rollouts:v1.7.2 2>/dev/null || true
kubectl rollout status deployment/argo-rollouts -n argo-rollouts --timeout=180s 2>/dev/null || true

echo "[setup] Argo Rollouts controller ready."

###############################################
# CAPTURE LIKE-SERVICE CURRENT STATE
###############################################
echo "[setup] Capturing like-service state..."

LIKE_IMAGE=$(kubectl get deployment like-service -n bleater -o jsonpath='{.spec.template.spec.containers[0].image}')
LIKE_LABELS=$(kubectl get deployment like-service -n bleater -o json | jq -r '.spec.selector.matchLabels | to_entries | map("\(.key): \(.value)") | join("\n    ")')
LIKE_REPLICAS=$(kubectl get deployment like-service -n bleater -o jsonpath='{.spec.replicas}')
LIKE_PORT=$(kubectl get deployment like-service -n bleater -o jsonpath='{.spec.template.spec.containers[0].ports[0].containerPort}' 2>/dev/null || echo "8006")

echo "[setup] like-service image: ${LIKE_IMAGE}"
echo "[setup] like-service replicas: ${LIKE_REPLICAS}"

###############################################
# BREAKAGE 1: SERVICEMONITOR WITH 60s SCRAPE
# The cost-reduction exercise changed scrapeInterval
# from 15s to 60s. This means rate() needs a window
# of at least 120s (2x scrape) to get data points.
###############################################
echo "[setup] BREAKAGE 1: Creating ServiceMonitor with 60s scrapeInterval..."

kubectl apply -f - <<EOF
apiVersion: monitoring.coreos.com/v1
kind: ServiceMonitor
metadata:
  name: like-service-monitor
  namespace: bleater
  labels:
    app: like-service
    team: platform
  annotations:
    change-ticket: "COST-2024-Q4-0312"
    description: "Scrape interval increased from 15s to 60s per cost-reduction initiative"
spec:
  selector:
    matchLabels:
      app: like-service
  endpoints:
  - port: http
    path: /metrics
    interval: "60s"
    scrapeTimeout: "30s"
EOF

###############################################
# BREAKAGE 2: ANALYSISTEMPLATE WITH BAD QUERY
# rate window (30s) < scrapeInterval (60s) = NaN
# Also missing nanStrategy field
###############################################
echo "[setup] BREAKAGE 2: Creating broken AnalysisTemplate..."

kubectl apply -f - <<'EOF'
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: like-service-error-rate
  namespace: bleater
  labels:
    app: like-service
    component: canary-analysis
  annotations:
    description: "Canary error rate analysis for like-service progressive delivery"
    created-by: "platform-team"
    last-reviewed: "2024-11-20"
spec:
  metrics:
  - name: error-rate
    interval: 30s
    count: 3
    successCondition: "result[0] < 0.05"
    failureCondition: "result[0] >= 0.10"
    provider:
      prometheus:
        address: http://prometheus.monitoring.svc.cluster.local:9090
        query: |
          sum(rate(http_requests_total{service="like-service",code=~"5.."}[30s]))
          /
          sum(rate(http_requests_total{service="like-service"}[30s]))
EOF

###############################################
# CONVERT LIKE-SERVICE TO ROLLOUT
# Scale down the Deployment, create a Rollout
# resource that manages the same pods
###############################################
echo "[setup] Converting like-service to Argo Rollout..."

# Scale down the original deployment
kubectl scale deployment like-service -n bleater --replicas=0 2>/dev/null || true

# Create the Rollout resource
kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: like-service
  namespace: bleater
  labels:
    app: like-service
  annotations:
    description: "Progressive delivery for like-service via Argo Rollouts"
    migrated-from: "Deployment"
    migration-date: "2026-03-15"
spec:
  replicas: ${LIKE_REPLICAS:-2}
  revisionHistoryLimit: 3
  selector:
    matchLabels:
      app: like-service
  template:
    metadata:
      labels:
        app: like-service
    spec:
      containers:
      - name: like-service
        image: ${LIKE_IMAGE}
        ports:
        - containerPort: ${LIKE_PORT:-8006}
        env:
        - name: CANARY_VERSION
          value: "v2.1.0"
  strategy:
    canary:
      canaryService: like-service-canary
      stableService: like-service
      analysis:
        templates:
        - templateName: like-service-error-rate
        startingStep: 1
      steps:
      - setWeight: 20
      - pause: {}
      - setWeight: 40
      - pause: {duration: 60}
      - setWeight: 60
      - pause: {duration: 60}
      - setWeight: 100
EOF

# Create canary service
kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: like-service-canary
  namespace: bleater
  labels:
    app: like-service
    role: canary
spec:
  selector:
    app: like-service
  ports:
  - port: ${LIKE_PORT:-8006}
    targetPort: ${LIKE_PORT:-8006}
    name: http
EOF

# Wait for rollout to start
echo "[setup] Waiting for Rollout to initialize..."
sleep 30

# The rollout should now be stuck at step 1 (pause after setWeight: 20)
# because the AnalysisRun keeps returning Inconclusive

###############################################
# DECOY DOCUMENTATION
###############################################
echo "[setup] Creating documentation ConfigMaps..."

kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: like-service-rollout-runbook
  namespace: bleater
  labels:
    app: like-service
    component: documentation
data:
  runbook.md: |
    # like-service Canary Rollout Runbook

    ## Overview
    like-service uses Argo Rollouts for progressive canary delivery.
    The canary strategy promotes through 20% -> 40% -> 60% -> 100% with
    AnalysisRun gates at each step.

    ## Common Issues

    ### Rollout stuck at a step
    If the rollout is stuck, check the AnalysisRun status:
      kubectl get analysisrun -n bleater -l rollout=like-service

    If the analysis is showing "Failed", check the Prometheus query.
    If "Running" for too long, the metric may not be returning data.

    ### Prometheus query troubleshooting
    The error rate query should return a value between 0 and 1.
    If it returns NaN, check:
    - Is Prometheus scraping like-service?
    - Does the metric exist? Check /api/v1/query?query=http_requests_total{service="like-service"}
    - Is the rate window appropriate for the scrape interval?

    Note: After the Q4 2024 cost-reduction changes, some scrape intervals
    were increased. Make sure the rate() window is compatible.

    ## Manual Promotion
    To manually promote past a paused step:
      kubectl argo rollouts promote like-service -n bleater

    ## Contacts
    - Platform team: #platform-eng on Mattermost
    - On-call: Check PagerDuty schedule
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: cost-reduction-notes-q4
  namespace: monitoring
  labels:
    component: documentation
data:
  notes.md: |
    # Q4 2024 Cost Reduction — Observability Changes

    ## Summary
    Reduced Prometheus resource consumption by ~30% through scrape interval
    optimization. Services with low-frequency metric changes were moved to
    longer scrape intervals.

    ## Changes Applied
    - bleater-api-gateway: 15s -> 30s
    - bleater-like-service: 15s -> 60s (low traffic service)
    - bleater-bleat-service: kept at 15s (high traffic)
    - bleater-profile-service: 15s -> 45s

    ## Known Impact
    - Dashboards may show gaps for services with longer intervals
    - rate() and irate() queries need windows >= 2x scrape interval
    - Some alerting rules may need query window adjustments

    ## Status: Completed
    All changes deployed. Monitoring for 30 days for anomalies.
    No issues reported as of 2025-01-15.
EOF

###############################################
# STRIP ANNOTATIONS
###############################################
echo "[setup] Stripping annotations..."
for res in \
  "analysistemplate/like-service-error-rate -n bleater" \
  "servicemonitor/like-service-monitor -n bleater" \
  "rollout/like-service -n bleater" \
  "configmap/like-service-rollout-runbook -n bleater" \
  "configmap/cost-reduction-notes-q4 -n monitoring"; do
  kubectl annotate ${res} kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
done

###############################################
# SAVE SETUP INFO
###############################################
cat > /root/.setup_info <<SETUP_EOF
LIKE_IMAGE=${LIKE_IMAGE}
LIKE_PORT=${LIKE_PORT:-8006}
LIKE_REPLICAS=${LIKE_REPLICAS:-2}
SETUP_EOF
chmod 600 /root/.setup_info

echo "[setup] ============================================"
echo "[setup] Setup complete. Canary rollout stuck at 20%."
echo "[setup] ============================================"
