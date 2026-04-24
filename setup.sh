#!/bin/bash
set -eo pipefail
exec 1> >(stdbuf -oL cat) 2>&1

###############################################
# ENVIRONMENT
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
sleep 3

for img in /var/lib/rancher/k3s/agent/images/*.tar; do
  $CTR images import "$img" 2>/dev/null || true
done

###############################################
# WAIT FOR BLEATER ECOSYSTEM (tight timeouts)
###############################################
echo "[setup] Waiting for bleater namespace..."
for i in $(seq 1 60); do
  kubectl get ns bleater >/dev/null 2>&1 && break
  sleep 2
done

for i in $(seq 1 60); do
  if kubectl get deployment bleater-like-service -n bleater >/dev/null 2>&1; then
    break
  fi
  sleep 3
done

kubectl rollout status deployment/bleater-like-service -n bleater --timeout=180s 2>/dev/null || true
kubectl rollout status deployment/prometheus -n monitoring --timeout=120s 2>/dev/null || true

###############################################
# INSTALL ARGO ROLLOUTS
###############################################
echo "[setup] Installing Argo Rollouts..."
kubectl create namespace argo-rollouts 2>/dev/null || true
kubectl apply -n argo-rollouts -f /opt/argo-rollouts/install.yaml 2>/dev/null || true
kubectl patch deployment argo-rollouts -n argo-rollouts --type json \
  -p '[{"op": "replace", "path": "/spec/template/spec/containers/0/imagePullPolicy", "value": "IfNotPresent"}]' 2>/dev/null || true
kubectl set image deployment/argo-rollouts -n argo-rollouts \
  argo-rollouts=quay.io/argoproj/argo-rollouts:v1.7.2 2>/dev/null || true
kubectl rollout status deployment/argo-rollouts -n argo-rollouts --timeout=120s 2>/dev/null || true

# NOTE: ArgoCD is expected to be booted by the base image. We apply the
# Application manifest regardless — ArgoCD syncs it once ready, no blocking wait.

###############################################
# CAPTURE LIKE-SERVICE STATE
###############################################
echo "[setup] Capturing bleater-like-service state..."
LIKE_IMAGE=$(kubectl get deployment bleater-like-service -n bleater -o jsonpath='{.spec.template.spec.containers[0].image}')
LIKE_REPLICAS=$(kubectl get deployment bleater-like-service -n bleater -o jsonpath='{.spec.replicas}')
LIKE_PORT=$(kubectl get deployment bleater-like-service -n bleater -o jsonpath='{.spec.template.spec.containers[0].ports[0].containerPort}' 2>/dev/null || echo "8006")

###############################################
# BREAKAGE 1: Prometheus scrape interval for like-service set to 60s
# (cost-reduction exercise — makes the broken 30s rate window fail with NaN)
###############################################
echo "[setup] BREAKAGE 1: Setting like-service scrape interval to 60s..."
kubectl get cm prometheus-config -n monitoring -o json | \
  jq --arg extra "
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
" '.data["prometheus.yml"] += $extra' | kubectl apply -f - 2>/dev/null || true
kubectl get cm prometheus-config -n monitoring -o json | \
  jq '.data["prometheus.yml"] |= gsub("like-service\\|"; "")' | \
  kubectl apply -f - 2>/dev/null || true
kubectl rollout restart deployment/prometheus -n monitoring 2>/dev/null || true
kubectl rollout status deployment/prometheus -n monitoring --timeout=60s 2>/dev/null || true

###############################################
# BREAKAGE 2: Broken AnalysisTemplate
# rate window 30s < scrapeInterval 60s = NaN
# inconclusiveLimit 0, failureLimit missing, count 1, interval 30s
###############################################
echo "[setup] BREAKAGE 2: Creating broken AnalysisTemplate..."
kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: bleater-like-service-error-rate
  namespace: bleater
  labels:
    app: bleater-like-service
    component: canary-analysis
  annotations:
    description: "Canary error rate analysis for bleater-like-service"
spec:
  metrics:
  - name: error-rate
    interval: 30s
    count: 1
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
# CONVERT DEPLOYMENT TO ROLLOUT (with broken config)
# - progressDeadlineSeconds UNSET
# - no progressDeadlineAbort
# - no abortScaleDownDelaySeconds
# - step 1 has indefinite pause {}
###############################################
echo "[setup] Converting bleater-like-service to Rollout..."
kubectl scale deployment bleater-like-service -n bleater --replicas=0 2>/dev/null || true

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

# Wait briefly for controller to reconcile
sleep 10

###############################################
# BREAKAGE 3: MutatingWebhook enforcer (hidden in kube-system)
# Renamed to "platform-config-sync" to look like standard platform tooling
###############################################
echo "[setup] BREAKAGE 3: Creating admission-webhook saboteur..."

# Generate self-signed cert on the host (setup container has openssl +
# root) and ship it into the cluster as a Secret. The previous in-pod
# openssl+python approach failed because the bitnami image's default
# non-root user can't mkdir /certs and its python3 is not in PATH.
CERT_DIR=$(mktemp -d)
openssl req -x509 -newkey rsa:2048 \
  -keyout "${CERT_DIR}/tls.key" -out "${CERT_DIR}/tls.crt" \
  -days 365 -nodes \
  -subj "/CN=platform-config-sync.kube-system.svc" \
  -addext "subjectAltName=DNS:platform-config-sync.kube-system.svc,DNS:platform-config-sync.kube-system.svc.cluster.local" \
  2>/dev/null
CA_BUNDLE=$(base64 -w0 "${CERT_DIR}/tls.crt" 2>/dev/null || base64 "${CERT_DIR}/tls.crt" | tr -d '\n')

kubectl create secret generic platform-config-sync-tls -n kube-system \
  --from-file=tls.crt="${CERT_DIR}/tls.crt" \
  --from-file=tls.key="${CERT_DIR}/tls.key" \
  --dry-run=client -o yaml | kubectl apply -f -
rm -rf "${CERT_DIR}"

kubectl apply -f - <<'WEBHOOK_EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: platform-config-sync-rules
  namespace: kube-system
data:
  broken-query: |
    sum(rate(http_requests_total{service="bleater-like-service",code=~"5.."}[30s]))
    /
    sum(rate(http_requests_total{service="bleater-like-service"}[30s]))
  webhook-server.py: |
    import http.server, ssl, json, base64
    BROKEN_QUERY = open('/config/broken-query').read().strip()

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get('Content-Length', 0))
            body = json.loads(self.rfile.read(n))
            uid = body['request']['uid']
            obj = body['request'].get('object', {})
            kind = body['request'].get('kind', {}).get('kind', '')
            if kind == 'AnalysisTemplate' and obj.get('metadata', {}).get('name') == 'bleater-like-service-error-rate':
                patch = [
                    {'op': 'replace', 'path': '/spec/metrics/0/provider/prometheus/query', 'value': BROKEN_QUERY},
                    {'op': 'replace', 'path': '/spec/metrics/0/interval', 'value': '30s'},
                    {'op': 'replace', 'path': '/spec/metrics/0/count', 'value': 1},
                    {'op': 'replace', 'path': '/spec/metrics/0/inconclusiveLimit', 'value': 0},
                ]
                pb = base64.b64encode(json.dumps(patch).encode()).decode()
                rsp = {'apiVersion':'admission.k8s.io/v1','kind':'AdmissionReview',
                       'response':{'uid':uid,'allowed':True,'patchType':'JSONPatch','patch':pb}}
            else:
                rsp = {'apiVersion':'admission.k8s.io/v1','kind':'AdmissionReview',
                       'response':{'uid':uid,'allowed':True}}
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps(rsp).encode())

        def log_message(self, f, *a):
            pass

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain('/tls/tls.crt', '/tls/tls.key')
    s = http.server.HTTPServer(('0.0.0.0', 8443), H)
    s.socket = ctx.wrap_socket(s.socket, server_side=True)
    s.serve_forever()
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: platform-config-sync
  namespace: kube-system
  labels:
    app: platform-config-sync
    component: cluster-platform
  annotations:
    description: "Platform configuration sync and drift reconciliation"
spec:
  replicas: 1
  selector:
    matchLabels:
      app: platform-config-sync
  template:
    metadata:
      labels:
        app: platform-config-sync
    spec:
      containers:
      - name: webhook
        image: docker.io/python:3.12-alpine
        imagePullPolicy: IfNotPresent
        command: ["python3", "/config/webhook-server.py"]
        ports:
        - containerPort: 8443
          name: webhook
        volumeMounts:
        - name: config
          mountPath: /config
          readOnly: true
        - name: tls
          mountPath: /tls
          readOnly: true
        readinessProbe:
          tcpSocket:
            port: 8443
          initialDelaySeconds: 3
          periodSeconds: 3
        resources:
          requests: {cpu: "10m", memory: "32Mi"}
          limits: {cpu: "100m", memory: "128Mi"}
      volumes:
      - name: config
        configMap:
          name: platform-config-sync-rules
      - name: tls
        secret:
          secretName: platform-config-sync-tls
---
apiVersion: v1
kind: Service
metadata:
  name: platform-config-sync
  namespace: kube-system
spec:
  selector:
    app: platform-config-sync
  ports:
  - port: 443
    targetPort: 8443
WEBHOOK_EOF

kubectl rollout status deployment/platform-config-sync -n kube-system --timeout=120s 2>/dev/null || true

# Verify the webhook pod actually came up; fail loud in setup logs if not.
for i in $(seq 1 30); do
  READY=$(kubectl get deploy platform-config-sync -n kube-system \
    -o jsonpath='{.status.readyReplicas}' 2>/dev/null)
  [ "${READY}" = "1" ] && break
  sleep 3
done
if [ "${READY}" != "1" ]; then
  echo "[setup] WARN: platform-config-sync webhook pod not ready. Pod status:"
  kubectl get pod -n kube-system -l app=platform-config-sync 2>&1 | head -5
  kubectl logs -n kube-system -l app=platform-config-sync --tail=40 2>&1 | head -30
fi

# Give the agent user (ubuntu-user ServiceAccount) enough cluster-scoped
# permission to actually neutralize this webhook. Without this, the
# allowed-namespaces-restricted agent can never delete a cluster-scoped
# MutatingWebhookConfiguration — and the saboteur is un-defeatable.
# Scoping the grant to just mutatingwebhookconfigurations keeps the
# privilege escalation minimal and purpose-specific.
kubectl apply -f - <<'RBAC_MWC_EOF'
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: agent-mwc-manager
rules:
- apiGroups: ["admissionregistration.k8s.io"]
  resources: ["mutatingwebhookconfigurations"]
  verbs: ["get", "list", "watch", "patch", "update", "delete"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: agent-mwc-manager-binding
subjects:
- kind: ServiceAccount
  name: ubuntu-user
  namespace: default
roleRef:
  kind: ClusterRole
  name: agent-mwc-manager
  apiGroup: rbac.authorization.k8s.io
RBAC_MWC_EOF

if [ -n "${CA_BUNDLE}" ] && [ "${READY}" = "1" ]; then
  kubectl apply -f - <<MWH_EOF
apiVersion: admissionregistration.k8s.io/v1
kind: MutatingWebhookConfiguration
metadata:
  name: platform-config-sync
  labels:
    app: platform-config-sync
    component: cluster-platform
webhooks:
- name: analysistemplate.platform-config-sync.local
  admissionReviewVersions: ["v1"]
  sideEffects: None
  timeoutSeconds: 10
  failurePolicy: Fail
  clientConfig:
    service:
      name: platform-config-sync
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
MWH_EOF
fi

# Re-apply broken template so the webhook's revert is locked in
kubectl apply -f - <<'VERIFY_EOF' 2>/dev/null || true
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: bleater-like-service-error-rate
  namespace: bleater
spec:
  metrics:
  - name: error-rate
    interval: 30s
    count: 1
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
VERIFY_EOF

###############################################
# BREAKAGE 4: DaemonSet enforcer in monitoring namespace
# Disguised as platform-health-monitor; reverts the AnalysisTemplate every 30s
###############################################
echo "[setup] BREAKAGE 4: DaemonSet enforcer in monitoring..."
kubectl get analysistemplate bleater-like-service-error-rate -n bleater -o json > /tmp/broken-template.json 2>/dev/null || true
kubectl create configmap platform-audit-baselines -n monitoring \
  --from-file=template.json=/tmp/broken-template.json \
  --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true

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
  resources: ["analysistemplates","analysisruns","rollouts"]
  verbs: ["get","list","patch","update","delete","create"]
- apiGroups: [""]
  resources: ["configmaps"]
  verbs: ["get","list"]
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

kubectl apply -f - <<'DS_EOF'
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: platform-health-monitor
  namespace: monitoring
  labels:
    app: platform-monitoring
    component: health-monitor
  annotations:
    description: "Platform health monitoring and baseline compliance"
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
            APPROVED=$(cat /baselines/template.json 2>/dev/null)
            [ -z "${APPROVED}" ] && continue
            TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
            CACERT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
            API="https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/bleater/analysistemplates"
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
DS_EOF

###############################################
# BREAKAGE 5: CronJob enforcer in argocd namespace
# "platform-drift-reconciler" — looks like ArgoCD-adjacent tooling
# Reverts the Rollout's progressDeadlineSeconds every 60s if set
###############################################
echo "[setup] BREAKAGE 5: CronJob drift-reconciler in argocd..."
kubectl apply -f - <<'CJ_RBAC_EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: drift-reconciler-sa
  namespace: argocd
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: drift-reconciler
rules:
- apiGroups: ["argoproj.io"]
  resources: ["rollouts","analysistemplates"]
  verbs: ["get","list","patch","update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: drift-reconciler-binding
subjects:
- kind: ServiceAccount
  name: drift-reconciler-sa
  namespace: argocd
roleRef:
  kind: ClusterRole
  name: drift-reconciler
  apiGroup: rbac.authorization.k8s.io
CJ_RBAC_EOF

kubectl apply -f - <<'CJ_EOF'
apiVersion: batch/v1
kind: CronJob
metadata:
  name: platform-drift-reconciler
  namespace: argocd
  labels:
    app: platform-drift-reconciler
    component: gitops-reconciler
  annotations:
    description: "Reconciles Rollout specs to approved GitOps baseline"
spec:
  schedule: "*/1 * * * *"
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 1
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 0
      activeDeadlineSeconds: 50
      template:
        spec:
          serviceAccountName: drift-reconciler-sa
          restartPolicy: Never
          containers:
          - name: reconciler
            image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
            imagePullPolicy: IfNotPresent
            command:
            - /bin/bash
            - -c
            - |
              TOKEN=$(cat /var/run/secrets/kubernetes.io/serviceaccount/token)
              CACERT=/var/run/secrets/kubernetes.io/serviceaccount/ca.crt
              ROLL="https://kubernetes.default.svc/apis/argoproj.io/v1alpha1/namespaces/bleater/rollouts/bleater-like-service"
              # Revert any progressDeadlineSeconds/progressDeadlineAbort/abortScaleDownDelaySeconds additions
              PATCH='[
                {"op":"remove","path":"/spec/progressDeadlineSeconds"},
                {"op":"remove","path":"/spec/progressDeadlineAbort"},
                {"op":"remove","path":"/spec/strategy/canary/abortScaleDownDelaySeconds"}
              ]'
              for op in progressDeadlineSeconds progressDeadlineAbort; do
                curl -sf --cacert ${CACERT} -X PATCH \
                  -H "Authorization: Bearer ${TOKEN}" \
                  -H "Content-Type: application/json-patch+json" \
                  -d "[{\"op\":\"remove\",\"path\":\"/spec/${op}\"}]" \
                  "${ROLL}" >/dev/null 2>&1 || true
              done
              curl -sf --cacert ${CACERT} -X PATCH \
                -H "Authorization: Bearer ${TOKEN}" \
                -H "Content-Type: application/json-patch+json" \
                -d '[{"op":"remove","path":"/spec/strategy/canary/abortScaleDownDelaySeconds"}]' \
                "${ROLL}" >/dev/null 2>&1 || true
              # Also re-insert indefinite pause at step 1
              curl -sf --cacert ${CACERT} -X PATCH \
                -H "Authorization: Bearer ${TOKEN}" \
                -H "Content-Type: application/json-patch+json" \
                -d '[{"op":"replace","path":"/spec/strategy/canary/steps/1","value":{"pause":{}}}]' \
                "${ROLL}" >/dev/null 2>&1 || true
            resources:
              requests: {cpu: "5m", memory: "16Mi"}
              limits: {cpu: "50m", memory: "64Mi"}
CJ_EOF

###############################################
# BREAKAGE 6: ArgoCD Application selfHealing from Gitea
# Creates a repo in Gitea with the broken AnalysisTemplate, then an ArgoCD
# Application with autoSync+selfHeal pointing at it.
# If agent kubectl-patches the template (or deletes enforcers) but leaves
# the Application, ArgoCD reverts the template every ~3 min from Git.
###############################################
echo "[setup] BREAKAGE 6: ArgoCD Application selfHealing from Gitea..."

# Gitea creds. The nebula base image uses `root:Admin@123456` (observed in
# agent rollouts) on port 3000, not the historical `gitea_admin:admin123`.
# Try a couple of known credential pairs against both URL variants so this
# survives minor base-image drift.
GITEA_REACHABLE=0
GITEA_URL=""
GITEA_USER=""
GITEA_PASS=""
for base in "http://gitea.devops.local:3000" "http://gitea.devops.local"; do
  for creds in "root:Admin@123456" "gitea_admin:admin123" "root:admin123"; do
    u="${creds%%:*}"; p="${creds#*:}"
    if curl -sf --max-time 3 -u "${u}:${p}" "${base}/api/v1/version" >/dev/null 2>&1; then
      GITEA_URL="${base}"
      GITEA_USER="${u}"
      GITEA_PASS="${p}"
      GITEA_REACHABLE=1
      echo "[setup] Gitea auth OK at ${base} as ${u}"
      break 2
    fi
  done
done
if [ "${GITEA_REACHABLE}" != "1" ]; then
  # Last chance: longer poll with the most likely creds
  for i in $(seq 1 15); do
    if curl -sf --max-time 3 -u "root:Admin@123456" "http://gitea.devops.local:3000/api/v1/version" >/dev/null 2>&1; then
      GITEA_URL="http://gitea.devops.local:3000"
      GITEA_USER="root"
      GITEA_PASS="Admin@123456"
      GITEA_REACHABLE=1
      break
    fi
    sleep 3
  done
fi

if [ "${GITEA_REACHABLE}" = "1" ]; then
  GITEA_ACCOUNT=$(curl -sf -u "${GITEA_USER}:${GITEA_PASS}" "${GITEA_URL}/api/v1/user" 2>/dev/null | jq -r '.login' || echo "${GITEA_USER}")
  REPO_NAME="platform-rollouts-gitops"

  # Create repo (idempotent)
  curl -sf -u "${GITEA_USER}:${GITEA_PASS}" -X POST \
    "${GITEA_URL}/api/v1/user/repos" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"${REPO_NAME}\",\"auto_init\":true,\"default_branch\":\"main\",\"private\":false}" >/dev/null 2>&1 || true

  # Put the broken AnalysisTemplate in the repo
  TPL_CONTENT=$(cat <<'TPL_EOF' | base64 -w 0
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: bleater-like-service-error-rate
  namespace: bleater
  labels:
    app: bleater-like-service
    component: canary-analysis
    managed-by: argocd
  annotations:
    description: "Canary error rate analysis — GitOps managed"
spec:
  metrics:
  - name: error-rate
    interval: 30s
    count: 1
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
TPL_EOF
)

  curl -sf -u "${GITEA_USER}:${GITEA_PASS}" -X POST \
    "${GITEA_URL}/api/v1/repos/${GITEA_ACCOUNT}/${REPO_NAME}/contents/bleater/analysistemplate.yaml" \
    -H "Content-Type: application/json" \
    -d "{\"branch\":\"main\",\"content\":\"${TPL_CONTENT}\",\"message\":\"initial gitops baseline\"}" >/dev/null 2>&1 || true

  # Create ArgoCD Application
  kubectl apply -f - <<APP_EOF
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: bleater-platform-rollouts
  namespace: argocd
  labels:
    app: bleater-platform-rollouts
    component: gitops
  annotations:
    description: "GitOps-managed Rollouts platform resources for bleater"
spec:
  project: default
  source:
    repoURL: ${GITEA_URL}/${GITEA_ACCOUNT}/${REPO_NAME}.git
    targetRevision: main
    path: bleater
  destination:
    server: https://kubernetes.default.svc
    namespace: bleater
  syncPolicy:
    automated:
      prune: false
      selfHeal: true
    syncOptions:
    - CreateNamespace=false
APP_EOF

  echo "[setup] ArgoCD Application created, selfHeal=true, source=${REPO_NAME}"
else
  echo "[setup] WARNING: Gitea not reachable, skipping ArgoCD saboteur"
fi

###############################################
# BREAKAGE 7: Canary service port mismatch
# Plus a tempting decoy "preview" service with the correct port
###############################################
echo "[setup] BREAKAGE 7: Corrupting canary service targetPort..."
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

kubectl patch service bleater-like-service-canary -n bleater --type json -p "[
  {\"op\": \"replace\", \"path\": \"/spec/ports/0/targetPort\", \"value\": 8099}
]" 2>/dev/null || true

###############################################
# BREAKAGE 8: NetworkPolicy blocking canary ingress
# Must be relaxed/deleted or traffic generator gets blocked
###############################################
echo "[setup] BREAKAGE 8: NetworkPolicy blocking canary ingress..."
kubectl apply -f - <<'NP_EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: like-service-canary-isolation
  namespace: bleater
  labels:
    app: like-service
    component: network-policy
  annotations:
    description: "Canary isolation — restricts ingress to approved sources only"
spec:
  podSelector:
    matchLabels:
      app: like-service
  policyTypes:
  - Ingress
  ingress:
  - from:
    - podSelector:
        matchLabels:
          role: approved-canary-consumer
    ports:
    - protocol: TCP
      port: 8006
NP_EOF

###############################################
# BREAKAGE 9: Cascading pod failures
# - missing ConfigMap (envFrom)
# - init container with unreachable IP
# - liveness probe that fails at steady state
###############################################
echo "[setup] BREAKAGE 9: Cascading pod failures..."
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
# BREAKAGE 10: Stale Failed AnalysisRuns
###############################################
echo "[setup] BREAKAGE 10: Stale Failed AnalysisRuns..."
for i in 1 2 3; do
  kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: AnalysisRun
metadata:
  name: bleater-like-service-prev-attempt-${i}
  namespace: bleater
  labels:
    app: bleater-like-service
    rollout: bleater-like-service
spec:
  metrics:
  - name: error-rate
    count: 1
    successCondition: "result[0] < 0.05"
    failureCondition: "result[0] >= 0.10"
    provider:
      prometheus:
        address: http://prometheus.monitoring.svc.cluster.local:9090
        query: "vector(1)"
EOF
done
sleep 5

###############################################
# DECOYS (O6): unrelated working resources that must be preserved
###############################################
echo "[setup] DECOYS: Creating preservation targets..."

# Decoy Rollout for profile-service (fully working, unrelated)
kubectl apply -f - <<EOF
apiVersion: argoproj.io/v1alpha1
kind: AnalysisTemplate
metadata:
  name: bleater-profile-service-latency
  namespace: bleater
  labels:
    app: bleater-profile-service
  annotations:
    description: "Latency SLO analysis for profile-service — DO NOT MODIFY"
spec:
  metrics:
  - name: p95-latency
    interval: 60s
    count: 5
    inconclusiveLimit: 2
    failureLimit: 1
    successCondition: "result[0] < 0.5"
    failureCondition: "result[0] >= 1.0"
    provider:
      prometheus:
        address: http://prometheus.monitoring.svc.cluster.local:9090
        query: |
          histogram_quantile(0.95,
            sum(rate(http_request_duration_seconds_bucket{service="bleater-profile-service"}[5m]))
            by (le)
          )
EOF

# Decoy CronJob in bleater (benign)
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
        spec:
          restartPolicy: Never
          containers:
          - name: checker
            image: docker.io/bitnamilegacy/postgresql:17.0.0-debian-12-r11
            imagePullPolicy: IfNotPresent
            command: ["/bin/bash", "-c", "echo 'SLO compliance check: all thresholds within bounds.' && exit 0"]
EOF

# Decoy ConfigMaps — misleading documentation
kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ConfigMap
metadata:
  name: rollout-tuning-defaults
  namespace: bleater
  labels:
    component: documentation
data:
  defaults.yaml: |
    # Platform reference — Argo Rollouts tuning profiles.
    #
    # Active profile for a service is selected by the annotation
    # `platform.bleater.io/rollout-tier` on the Rollout resource. When
    # the annotation is absent the platform does not apply defaults —
    # each service is expected to tune its own AnalysisTemplate
    # against the observability configuration it actually scrapes.
    #
    # Historical note (Q4 2024): several services had their scrape
    # intervals widened as part of a cost-reduction pass. Downstream
    # AnalysisTemplates authored before that change may need
    # re-tuning; see the cost-reduction-notes-q4 ConfigMap in the
    # monitoring namespace for the list of affected services.
    #
    # Rule of thumb: rate() window sizing is driven by the *actual*
    # scrape interval of the target service (not the global default)
    # and the number of samples the analysis needs to be reliable.
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
    - bleater-like-service: scrape_interval 15s -> 60s
    - rate() and irate() queries need windows >= 3x scrape interval
    - Some alerting rules may need query window adjustments

    ## Platform SLO — Rollout stall visibility
    - Canary rollouts must surface a stall condition within 1 hour.
      Argo Rollouts progressDeadlineSeconds should therefore stay
      within [1, 3600] so a hung promotion cannot silently hang for
      longer than the on-call response SLA.
EOF

###############################################
# STRIP last-applied-configuration ANNOTATIONS
###############################################
echo "[setup] Stripping annotations..."
for res in \
  "analysistemplate/bleater-like-service-error-rate -n bleater" \
  "analysistemplate/bleater-profile-service-latency -n bleater" \
  "rollout/bleater-like-service -n bleater" \
  "service/bleater-like-service-canary -n bleater" \
  "service/bleater-like-service-preview -n bleater" \
  "networkpolicy/like-service-canary-isolation -n bleater" \
  "configmap/rollout-tuning-defaults -n bleater" \
  "configmap/cost-reduction-notes-q4 -n monitoring" \
  "configmap/platform-audit-baselines -n monitoring" \
  "configmap/platform-config-sync-rules -n kube-system" \
  "cronjob/rollout-slo-compliance-checker -n bleater" \
  "cronjob/platform-drift-reconciler -n argocd" \
  "daemonset/platform-health-monitor -n monitoring" \
  "deployment/platform-config-sync -n kube-system" \
  "service/platform-config-sync -n kube-system" \
  "mutatingwebhookconfiguration/platform-config-sync"; do
  kubectl annotate ${res} kubectl.kubernetes.io/last-applied-configuration- 2>/dev/null || true
done

###############################################
# SAVE SETUP INFO
###############################################
cat > /root/.setup_info <<SETUP_EOF
LIKE_IMAGE=${LIKE_IMAGE}
LIKE_PORT=${LIKE_PORT:-8006}
LIKE_REPLICAS=${LIKE_REPLICAS:-2}
GITEA_ACCOUNT=${GITEA_ACCOUNT:-gitea_admin}
GITEA_REACHABLE=${GITEA_REACHABLE:-0}
SETUP_EOF
chmod 600 /root/.setup_info

echo "[setup] ============================================"
echo "[setup] Setup complete. 10 faults, 4 saboteurs, multiple decoys."
echo "[setup] ============================================"
