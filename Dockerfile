FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.1.0

ENV DISPLAY_NUM=1
ENV COMPUTER_HEIGHT_PX=768
ENV COMPUTER_WIDTH_PX=1024

ENV SKIP_BLEATER_BOOT=0
ENV ALLOWED_NAMESPACES="bleater,monitoring,argocd"
ENV ENABLE_ISTIO_BLEATER=false

# Install build tools
RUN apt-get update -qq && \
    apt-get install -y -qq skopeo curl && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Pull Argo Rollouts controller image for air-gapped k3s
RUN mkdir -p /var/lib/rancher/k3s/agent/images && \
    skopeo copy --override-os linux --override-arch amd64 \
      docker://quay.io/argoproj/argo-rollouts:v1.7.2 \
      docker-archive:/var/lib/rancher/k3s/agent/images/argo-rollouts.tar:quay.io/argoproj/argo-rollouts:v1.7.2

# Pull python:3.12-alpine for the admission-webhook saboteur. The previous
# bitnamilegacy/postgresql image had no python3 in PATH and refused to
# `mkdir /certs` as non-root, so the webhook Deployment crashloop'd and the
# MutatingWebhookConfiguration never registered in the eval environment.
RUN skopeo copy --override-os linux --override-arch amd64 \
      docker://docker.io/python:3.12-alpine \
      docker-archive:/var/lib/rancher/k3s/agent/images/python-webhook.tar:docker.io/python:3.12-alpine

# Download kubectl argo rollouts plugin
RUN curl -sLO https://github.com/argoproj/argo-rollouts/releases/download/v1.7.2/kubectl-argo-rollouts-linux-amd64 && \
    chmod +x kubectl-argo-rollouts-linux-amd64 && \
    mv kubectl-argo-rollouts-linux-amd64 /usr/local/bin/kubectl-argo-rollouts

# Download Argo Rollouts install manifests
RUN mkdir -p /opt/argo-rollouts && \
    curl -sL -o /opt/argo-rollouts/install.yaml \
      https://raw.githubusercontent.com/argoproj/argo-rollouts/v1.7.2/manifests/install.yaml
