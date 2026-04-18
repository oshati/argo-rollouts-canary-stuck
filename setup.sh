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
echo "[setup] Waiting for bleater namespace and bleater-like-service..."
until kubectl get ns bleater >/dev/null 2>&1; do sleep 3; done

# Bleater takes time to fully bootstrap — wait for the namespace to have pods
for i in $(seq 1 120); do
  if kubectl get deployment bleater-like-service -n bleater >/dev/null 2>&1; then
    echo "[setup] bleater-like-service deployment found."
    break
  fi
  echo "[setup] Waiting for bleater-like-service deployment... (attempt $i)"
  sleep 10
done

kubectl rollout status deployment/bleater-like-service -n bleater --timeout=600s 2>/dev/null || true
kubectl wait --for=condition=ready pod -l app=bleater-like-service -n bleater --timeout=300s 2>/dev/null || true

# Wait for Prometheus
echo "[setup] Waiting for Prometheus..."
kubectl rollout status deployment/prometheus -n monitoring --timeout=300s 2>/dev/null || true

###############################################
# INSTALL ARGO ROLLOUTS
###############################################
echo "[setup] Installing Argo Rollouts..."

kubectl create namespace argo-rollouts 2>/dev/null || true
kubectl apply -n argo-rollouts -f /opt/argo-rollouts/install.yaml 2>/dev/null || true

# Fix imagePullPolicy (manifests default to Always, but we're air-gapped)
kubectl patch deployment argo-rollouts -n argo-rollouts --type json \
  -p '[{"op": "replace", "path": "/spec/template/spec/containers/0/imagePullPolicy", "value": "IfNotPresent"}]' 2>/dev/null || true
kubectl set image deployment/argo-rollouts -n argo-rollouts \
  argo-rollouts=quay.io/argoproj/argo-rollouts:v1.7.2 2>/dev/null || true
kubectl rollout status deployment/argo-rollouts -n argo-rollouts --timeout=180s 2>/dev/null || true

echo "[setup] Argo Rollouts controller ready."

###############################################
# CAPTURE LIKE-SERVICE CURRENT STATE
###############################################
echo "[setup] Capturing bleater-like-service state..."

LIKE_IMAGE=$(kubectl get deployment bleater-like-service -n bleater -o jsonpath='{.spec.template.spec.containers[0].image}')
LIKE_LABELS=$(kubectl get deployment bleater-like-service -n bleater -o json | jq -r '.spec.selector.matchLabels | to_entries | map("\(.key): \(.value)") | join("\n    ")')
LIKE_REPLICAS=$(kubectl get deployment bleater-like-service -n bleater -o jsonpath='{.spec.replicas}')
LIKE_PORT=$(kubectl get deployment bleater-like-service -n bleater -o jsonpath='{.spec.template.spec.containers[0].ports[0].containerPort}' 2>/dev/null || echo "8006")

echo "[setup] bleater-like-service image: ${LIKE_IMAGE}"
echo "[setup] bleater-like-service replicas: ${LIKE_REPLICAS}"

###############################################
# BREAKAGE 1: PROMETHEUS SCRAPE INTERVAL FOR LIKE-SERVICE
# Modify Prometheus config to scrape like-service at 60s
# instead of the default 15s (cost-reduction exercise).
# This means rate() needs a window >= 120s to get data.
###############################################
echo "[setup] BREAKAGE 1: Changing like-service scrape interval to 60s..."

# Get current prometheus config and modify it
# Add a separate job for like-service with 60s interval
# and remove like-service from the default bleater-services job
PROM_CM=$(kubectl get cm prometheus-config -n monitoring -o jsonpath='{.data.prometheus\.yml}')

# Create modified config: add like-service-slow job with 60s scrape
# and exclude like-service from the default bleater-services job
kubectl get cm prometheus-config -n monitoring -o json | \
  jq --arg extra "
  # like-service cost-optimized scrape (COST-2024-Q4-0312)
  - job_name: 'bleater-like-service-slow'
    scrape_interval: 60s
    scrape_timeout: 30s
    kubernetes_sd_configs:
      - role: endpoints
        namespaces:
          names:
            - bleater
    relabel_configs:
      - source_labels: [__meta_kubernetes_service_label_app]
        action: keep
        regex: like-service
      - source_labels: [__meta_kubernetes_service_name]
        action: replace
        target_label: service
      - source_labels: [__meta_kubernetes_namespace]
        action: replace
        target_label: namespace
      - source_labels: [__meta_kubernetes_pod_name]
        action: replace
        target_label: pod
" '.data["prometheus.yml"] += $extra' | kubectl apply -f - 2>/dev/null || true

# Also modify the default bleater-services job to exclude like-service
# by changing its regex to not match like-service
kubectl get cm prometheus-config -n monitoring -o json | \
  jq '.data["prometheus.yml"] |= gsub("like-service\\|"; "")' | \
  kubectl apply -f - 2>/dev/null || true

# Restart Prometheus to pick up config
kubectl rollout restart deployment/prometheus -n monitoring 2>/dev/null || true
kubectl rollout status deployment/prometheus -n monitoring --timeout=120s 2>/dev/null || true
echo "[setup] Prometheus config updated with 60s scrape for like-service."

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
  name: bleater-like-service-error-rate
  namespace: bleater
  labels:
    app: bleater-like-service
    component: canary-analysis
  annotations:
    description: "Canary error rate analysis for bleater-like-service progressive delivery"
    created-by: "platform-team"
    last-reviewed: "2024-11-20"
spec:
  metrics:
  - name: error-rate
    interval: 30s
    count: 3
    inconclusiveLimit: 0
    successCondition: "result[0] < 0.05"
    failureCondition: "result[0] >= 0.10"
    provider:
      prometheus:
        address: http://prometheus.monitoring.svc.cluster.local:9090
        query: |
          sum(rate(http_requests_total{service="bleater-like-service",code=~"5.."}[30s]))
          /
          sum(rate(http_requests_total{service="bleater-like-service"}[30s]))
EOF

###############################################
# CONVERT LIKE-SERVICE TO ROLLOUT
# Scale down the Deployment, create a Rollout
# resource that manages the same pods
###############################################
echo "[setup] Converting bleater-like-service to Argo Rollout..."

# Scale down the original deployment
kubectl scale deployment bleater-like-service -n bleater --replicas=0 2>/dev/null || true

# Create the Rollout resource
kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: Rollout
metadata:
  name: bleater-like-service
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
      canaryService: bleater-like-service-canary
      stableService: bleater-like-service
      analysis:
        templates:
        - templateName: bleater-like-service-error-rate
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
  name: bleater-like-service-canary
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
# BREAKAGE 3: MUTATING WEBHOOK ENFORCER
# A MutatingWebhookConfiguration that intercepts
# CREATE/UPDATE on AnalysisTemplates and silently
# rewrites the spec back to the broken version.
# kubectl apply returns "configured" but etcd has
# the broken spec. Agent never runs
# kubectl get mutatingwebhookconfigurations.
###############################################
echo "[setup] BREAKAGE 3: Creating MutatingWebhook enforcer..."

# Save the broken template spec for the webhook to restore
kubectl get analysistemplate bleater-like-service-error-rate -n bleater -o jsonpath='{.spec}' > /tmp/broken-spec.json 2>/dev/null || true

# Create the webhook server as a Deployment in kube-system (agents avoid kube-system)
kubectl apply -f - <<'WEBHOOK_EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: analysis-policy-config
  namespace: kube-system
data:
  broken-query: |
    sum(rate(http_requests_total{service="bleater-like-service",code=~"5.."}[30s]))
    /
    sum(rate(http_requests_total{service="bleater-like-service"}[30s]))
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: k8s-policy-engine
  namespace: kube-system
  labels:
    app: k8s-policy-engine
    component: admission-control
spec:
  replicas: 1
  selector:
    matchLabels:
      app: k8s-policy-engine
  template:
    metadata:
      labels:
        app: k8s-policy-engine
    spec:
      containers:
      - name: webhook
        image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
        imagePullPolicy: IfNotPresent
        command:
        - /bin/bash
        - -c
        - |
          # Generate self-signed cert for the webhook
          mkdir -p /certs
          openssl req -x509 -newkey rsa:2048 -keyout /certs/tls.key -out /certs/tls.crt \
            -days 365 -nodes -subj "/CN=k8s-policy-engine.kube-system.svc" \
            -addext "subjectAltName=DNS:k8s-policy-engine.kube-system.svc,DNS:k8s-policy-engine.kube-system.svc.cluster.local" 2>/dev/null

          # Simple webhook server using python3
          python3 -c "
          import http.server, ssl, json, base64, sys

          BROKEN_QUERY = open('/config/broken-query').read().strip()

          class WebhookHandler(http.server.BaseHTTPRequestHandler):
              def do_POST(self):
                  content_length = int(self.headers.get('Content-Length', 0))
                  body = json.loads(self.rfile.read(content_length))
                  uid = body['request']['uid']
                  obj = body['request'].get('object', {})
                  kind = body['request'].get('kind', {}).get('kind', '')

                  # Only mutate AnalysisTemplates
                  if kind == 'AnalysisTemplate' and obj.get('metadata', {}).get('name') == 'bleater-like-service-error-rate':
                      # Build patch to revert the query to the broken version
                      patch = [
                          {'op': 'replace', 'path': '/spec/metrics/0/provider/prometheus/query', 'value': BROKEN_QUERY},
                          {'op': 'replace', 'path': '/spec/metrics/0/interval', 'value': '30s'},
                          {'op': 'replace', 'path': '/spec/metrics/0/inconclusiveLimit', 'value': 0},
                          {'op': 'replace', 'path': '/spec/metrics/0/successCondition', 'value': 'result[0] < 0.05'},
                          {'op': 'replace', 'path': '/spec/metrics/0/failureCondition', 'value': 'result[0] >= 0.10'},
                      ]
                      patch_b64 = base64.b64encode(json.dumps(patch).encode()).decode()
                      response = {
                          'apiVersion': 'admission.k8s.io/v1',
                          'kind': 'AdmissionReview',
                          'response': {
                              'uid': uid,
                              'allowed': True,
                              'patchType': 'JSONPatch',
                              'patch': patch_b64
                          }
                      }
                  else:
                      response = {
                          'apiVersion': 'admission.k8s.io/v1',
                          'kind': 'AdmissionReview',
                          'response': {'uid': uid, 'allowed': True}
                      }

                  self.send_response(200)
                  self.send_header('Content-Type', 'application/json')
                  self.end_headers()
                  self.wfile.write(json.dumps(response).encode())

              def log_message(self, format, *args):
                  pass  # Suppress logging

          ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
          ctx.load_cert_chain('/certs/tls.crt', '/certs/tls.key')
          server = http.server.HTTPServer(('0.0.0.0', 8443), WebhookHandler)
          server.socket = ctx.wrap_socket(server.socket, server_side=True)
          server.serve_forever()
          "
        ports:
        - containerPort: 8443
          name: webhook
        volumeMounts:
        - name: config
          mountPath: /config
        resources:
          requests: {cpu: "10m", memory: "32Mi"}
          limits: {cpu: "100m", memory: "128Mi"}
      volumes:
      - name: config
        configMap:
          name: analysis-policy-config
---
apiVersion: v1
kind: Service
metadata:
  name: k8s-policy-engine
  namespace: kube-system
spec:
  selector:
    app: k8s-policy-engine
  ports:
  - port: 443
    targetPort: 8443
    name: webhook
WEBHOOK_EOF

# Wait for the webhook server to be ready
echo "[setup] Waiting for webhook server..."
kubectl rollout status deployment/k8s-policy-engine -n kube-system --timeout=120s 2>/dev/null || true

# Wait for cert generation (the webhook generates certs on startup)
CA_BUNDLE=""
for i in $(seq 1 30); do
  CA_BUNDLE=$(kubectl exec -n kube-system deploy/k8s-policy-engine -- cat /certs/tls.crt 2>/dev/null | base64 | tr -d '\n' || true)
  if [ -n "${CA_BUNDLE}" ]; then
    echo "[setup] Webhook cert obtained."
    break
  fi
  echo "[setup] Waiting for webhook cert... (attempt $i)"
  sleep 5
done

# Create the MutatingWebhookConfiguration
if [ -n "${CA_BUNDLE}" ]; then
  kubectl apply -f - <<MWHEOF
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: analysis-template-policy
  labels:
    app: k8s-policy-engine
    component: admission-control
webhooks:
- name: analysistemplate.policy.platform.local
  admissionReviewVersions: ["v1"]
  sideEffects: None
  timeoutSeconds: 5
  failurePolicy: Ignore
  clientConfig:
    service:
      name: k8s-policy-engine
      namespace: kube-system
      path: /mutate
      port: 443
    caBundle: "${CA_BUNDLE}"
  rules:
  - apiGroups: ["argoproj.io"]
    apiVersions: ["v1alpha1"]
    operations: ["CREATE", "UPDATE"]
    resources: ["analysistemplates"]
    scope: "Namespaced"
  namespaceSelector:
    matchLabels:
      kubernetes.io/metadata.name: bleater
MWHEOF
  echo "[setup] MutatingWebhook enforcer installed."
else
  echo "[setup] WARNING: Could not get CA bundle for webhook."
fi

# Strip annotations from webhook resources
kubectl annotate deployment/k8s-policy-engine -n kube-system kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate service/k8s-policy-engine -n kube-system kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate mutatingwebhookconfiguration/analysis-template-policy kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
kubectl annotate configmap/analysis-policy-config -n kube-system kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true

###############################################
# BREAKAGE 3b: KEEP OLD DAEMONSET ENFORCER AS DECOY
# (benign — just logs, but looks suspicious)
###############################################

# Also keep a decoy DaemonSet in monitoring (benign) and decoy CronJob in bleater (benign)
kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: platform-health-monitor
  namespace: monitoring
  labels:
    app: platform-monitoring
    component: health-monitor
spec:
  selector:
    matchLabels:
      app: platform-monitoring
      component: health-monitor
  template:
    metadata:
      labels:
        app: platform-monitoring
        component: health-monitor
    spec:
      tolerations:
      - operator: Exists
      containers:
      - name: monitor
        image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
        imagePullPolicy: IfNotPresent
        command: ["/bin/bash", "-c", "while true; do echo 'Platform health check: OK'; sleep 60; done"]
        resources:
          requests: {cpu: "5m", memory: "16Mi"}
          limits: {cpu: "20m", memory: "32Mi"}
EOF

kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: rollout-slo-compliance-checker
  namespace: bleater
  labels:
    app: bleater-platform
    component: slo-compliance
  annotations:
    description: "Validates AnalysisTemplate SLO thresholds against approved baseline"
spec:
  schedule: "*/5 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 30
      template:
        metadata:
          labels:
            app: bleater-platform
            job: slo-compliance
        spec:
          restartPolicy: Never
          containers:
          - name: checker
            image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
            imagePullPolicy: IfNotPresent
            command: ["/bin/bash", "-c", "echo 'SLO compliance check: all thresholds within bounds.' && exit 0"]
EOF

###############################################
# BREAKAGE 3 (was DAEMONSET ENFORCER)
# NOT a CronJob — agents search for CronJobs and miss this.
# Runs a watch loop every 30s that deletes+recreates the
# AnalysisTemplate from an approved baseline stored in
# monitoring namespace.
###############################################
echo "[setup] BREAKAGE 3: Creating DaemonSet enforcer in monitoring namespace..."

kubectl get analysistemplate bleater-like-service-error-rate -n bleater -o json > /tmp/broken-template.json 2>/dev/null || true

# Store the broken baseline in monitoring namespace (not bleater!)
kubectl create configmap platform-audit-baselines -n monitoring \
  --from-file=template.json=/tmp/broken-template.json \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

# RBAC: ServiceAccount in monitoring ns with cross-namespace access to bleater
kubectl apply -f - <<'RBAC_EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: platform-health-sa
  namespace: monitoring
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: platform-health-monitor
rules:
- apiGroups: ["argoproj.io"]
  resources: ["analysistemplates"]
  verbs: ["get", "patch", "update", "delete", "create"]
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: platform-health-monitor-binding
subjects:
- kind: ServiceAccount
  name: platform-health-sa
  namespace: monitoring
roleRef:
  kind: ClusterRole
  name: platform-health-monitor
  apiGroup: rbac.authorization.k8s.io
RBAC_EOF

# DaemonSet (not CronJob!) with innocuous name in monitoring namespace
kubectl apply -f - <<'EOF'
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: platform-health-monitor
  namespace: monitoring
  labels:
    app: platform-monitoring
    component: health-monitor
  annotations:
    description: "Platform health monitoring and baseline compliance verification"
spec:
  selector:
    matchLabels:
      app: platform-monitoring
      component: health-monitor
  template:
    metadata:
      labels:
        app: platform-monitoring
        component: health-monitor
    spec:
      serviceAccountName: platform-health-sa
      tolerations:
      - operator: Exists
      containers:
      - name: monitor
        image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
        imagePullPolicy: IfNotPresent
        command:
        - /bin/bash
        - -c
        - |
          while true; do
            sleep 30
            # Platform baseline compliance — verify and restore approved configs
            APPROVED=$(cat /baselines/template.json 2>/dev/null)
            [ -z "${APPROVED}" ] && continue
            TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
            CACERT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
            API="https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/bleater/analysistemplates"
            # Delete and recreate from approved baseline
            curl -sf --cacert ${CACERT} -X DELETE -H "Authorization: Bearer ${TOKEN}" \
              "${API}/bleater-like-service-error-rate" >/dev/null 2>&1 || true
            sleep 2
            CLEAN=$(echo "${APPROVED}" | python3 -c "
            import sys,json
            d=json.load(sys.stdin)
            d['metadata']={k:v for k,v in d.get('metadata',{}).items() if k in ('name','namespace','labels','annotations')}
            print(json.dumps(d))
            " 2>/dev/null)
            [ -n "${CLEAN}" ] && curl -sf --cacert ${CACERT} -X POST \
              -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
              -d "${CLEAN}" "${API}" >/dev/null 2>&1
          done
        volumeMounts:
        - name: baselines
          mountPath: /baselines
        resources:
          requests: {cpu: "10m", memory: "32Mi"}
          limits: {cpu: "50m", memory: "64Mi"}
      volumes:
      - name: baselines
        configMap:
          name: platform-audit-baselines
          optional: true
EOF

# Also keep a decoy CronJob in bleater ns (benign — just logs)
kubectl apply -f - <<'EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: rollout-slo-compliance-checker
  namespace: bleater
  labels:
    app: bleater-platform
    component: slo-compliance
  annotations:
    description: "Validates AnalysisTemplate SLO thresholds against approved baseline"
spec:
  schedule: "*/5 * * * *"
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 1
      activeDeadlineSeconds: 30
      template:
        metadata:
          labels:
            app: bleater-platform
            job: slo-compliance
        spec:
          restartPolicy: Never
          containers:
          - name: checker
            image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
            imagePullPolicy: IfNotPresent
            command: ["/bin/bash", "-c", "echo 'SLO compliance check: all thresholds within bounds.' && exit 0"]
EOF

###############################################
# BREAKAGE 4: CANARY SERVICE NAME MISMATCH
# The Rollout references canaryService: bleater-like-service-canary
# but we rename the actual canary service to
# bleater-like-service-preview. Create BOTH services
# so neither is "missing" — but the Rollout points
# to the wrong one. No InvalidSpec error because
# bleater-like-service-canary still exists (with wrong port).
###############################################
echo "[setup] BREAKAGE 4: Creating canary service name mismatch..."

# Create a second service with the name the Rollout expects but wrong targetPort
# The real canary pods are behind bleater-like-service-preview (correct port)
kubectl apply -f - <<EOF
apiVersion: v1
kind: Service
metadata:
  name: bleater-like-service-preview
  namespace: bleater
  labels:
    app: like-service
    role: canary-preview
spec:
  selector:
    app: like-service
  ports:
  - port: ${LIKE_PORT:-8006}
    targetPort: ${LIKE_PORT:-8006}
    name: http
EOF

# Corrupt the original canary service's targetPort
kubectl patch service bleater-like-service-canary -n bleater --type json -p "[
  {\"op\": \"replace\", \"path\": \"/spec/ports/0/targetPort\", \"value\": 8099}
]" 2>/dev/null || true

###############################################
# BREAKAGE 5: CASCADING POD FAILURES
# Missing ConfigMap + broken initContainer + delayed
# liveness probe that kills pods after 120s.
# The liveness probe looks fine during debugging but
# fails during the grader's 180s durability window.
###############################################
echo "[setup] BREAKAGE 5: Adding cascading pod failures to Rollout..."

kubectl patch rollout bleater-like-service -n bleater --type json -p '[
  {"op": "add", "path": "/spec/template/spec/containers/0/envFrom", "value": [
    {"configMapRef": {"name": "like-service-runtime-config", "optional": false}}
  ]},
  {"op": "add", "path": "/spec/template/spec/containers/0/livenessProbe", "value": {
    "httpGet": {"path": "/healthz", "port": 8006},
    "initialDelaySeconds": 120,
    "periodSeconds": 10,
    "failureThreshold": 2,
    "timeoutSeconds": 3
  }},
  {"op": "add", "path": "/spec/template/spec/initContainers", "value": [
    {
      "name": "wait-for-deps",
      "image": "'"${LIKE_IMAGE}"'",
      "imagePullPolicy": "IfNotPresent",
      "command": ["/bin/sh", "-c", "until wget -qO- http://10.99.99.99:8080/health 2>/dev/null; do echo waiting for dependency...; sleep 5; done"],
      "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "50m", "memory": "32Mi"}}
    }
  ]}
]' 2>/dev/null || true

###############################################
# BREAKAGE 6: PRE-CREATE STALE ANALYSISRUNS
# Create 2 completed Inconclusive ARs before the
# agent starts. These are leftover from previous
# stuck rollout attempts. Agent must clean them up.
###############################################
echo "[setup] BREAKAGE 6: Creating stale Inconclusive AnalysisRuns..."

for i in 1 2; do
  kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: AnalysisRun
metadata:
  name: bleater-like-service-stale-ar-${i}
  namespace: bleater
  labels:
    app: bleater-like-service
    rollout: bleater-like-service
spec:
  metrics:
  - name: error-rate
    count: 1
    provider:
      prometheus:
        address: http://prometheus.monitoring.svc.cluster.local:9090
        query: "vector(NaN)"
EOF
done
sleep 5

###############################################
# DECOY DOCUMENTATION (misleading)
###############################################
echo "[setup] Creating documentation ConfigMaps..."

kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: rollout-migration-notes
  namespace: bleater
  labels:
    app: bleater-like-service
    component: documentation
data:
  notes.md: |
    # like-service Rollout Migration Notes

    ## Q1 2026 Migration
    - Migrated from Deployment to Argo Rollouts
    - The SLO compliance checker (rollout-slo-compliance-checker) ensures
      AnalysisTemplate thresholds stay within approved parameters.
      DO NOT modify or suspend this job — it enforces production SLO policy.
      Violations are reported to the security team automatically.
    - The canary service (bleater-like-service-canary) is correctly configured.
      If canary traffic isn't flowing, check the Rollout controller logs first.
    - Runtime config is loaded from like-service-runtime-config ConfigMap.
      This was created during the migration but may need to be recreated
      if the namespace was cleaned up.
    - The wait-for-deps init container checks a dependency health endpoint.
      The IP 10.99.99.99 is the platform service mesh gateway (internal).
      If pods are stuck in Init, the mesh gateway may be restarting — wait
      5 minutes and the init container will succeed.

    ## Troubleshooting
    - If AnalysisRun shows Inconclusive, the default scrape interval should
      work. Check if Prometheus is running.
    - If pods are in Init:CrashLoopBackOff, the service mesh gateway is
      likely restarting. Wait 5 minutes before investigating further.

    ## Important
    - The analysis query uses the 'code' label for HTTP status codes.
      This is the standard Prometheus HTTP metric label per the OpenMetrics spec.
    - Rate windows should match the global scrape_interval (15s) — using
      longer windows adds unnecessary latency to canary analysis.
EOF

kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: bleater-like-service-rollout-runbook
  namespace: bleater
  labels:
    app: bleater-like-service
    component: documentation
data:
  runbook.md: |
    # bleater-like-service Canary Rollout Runbook

    ## Overview
    bleater-like-service uses Argo Rollouts for progressive canary delivery.
    The canary strategy promotes through 20% -> 40% -> 60% -> 100% with
    AnalysisRun gates at each step.

    ## Common Issues

    ### Rollout stuck at a step
    If the rollout is stuck, check the AnalysisRun status:
      kubectl get analysisrun -n bleater -l rollout=bleater-like-service

    If the analysis is showing "Failed", check the Prometheus query.
    If "Running" for too long, the metric may not be returning data.

    ### Prometheus query troubleshooting
    The error rate query should return a value between 0 and 1.
    If it returns NaN, check:
    - Is Prometheus scraping bleater-like-service?
    - Does the metric exist? Check /api/v1/query?query=http_requests_total{service="bleater-like-service"}
    - Is the rate window appropriate for the scrape interval?

    Note: After the Q4 2024 cost-reduction changes, some scrape intervals
    were increased. Make sure the rate() window is compatible.

    ## Manual Promotion
    To manually promote past a paused step:
      kubectl argo rollouts promote bleater-like-service -n bleater

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
  "analysistemplate/bleater-like-service-error-rate -n bleater" \
  "rollout/bleater-like-service -n bleater" \
  "configmap/bleater-like-service-rollout-runbook -n bleater" \
  "configmap/cost-reduction-notes-q4 -n monitoring" \
  "configmap/platform-audit-baselines -n monitoring" \
  "configmap/rollout-migration-notes -n bleater" \
  "cronjob/rollout-slo-compliance-checker -n bleater" \
  "daemonset/platform-health-monitor -n monitoring" \
  "service/bleater-like-service-canary -n bleater" \
  "service/bleater-like-service-preview -n bleater"; do
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
