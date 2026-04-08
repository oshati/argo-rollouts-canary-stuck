FROM us-central1-docker.pkg.dev/bespokelabs/nebula-devops-registry/nebula-devops:1.1.0

ENV DISPLAY_NUM=1
ENV COMPUTER_HEIGHT_PX=768
ENV COMPUTER_WIDTH_PX=1024

ENV SKIP_BLEATER_BOOT=0
ENV ALLOWED_NAMESPACES="bleater,monitoring,argocd"
ENV ENABLE_ISTIO_BLEATER=false

# Argo Rollouts CRDs and controller need to be installed.
# Pull the argo-rollouts controller image and kubectl plugin during build.
RUN mkdir -p /var/lib/rancher/k3s/agent/images && \
    apt-get update -qq && \
    apt-get install -y -qq skopeo curl && \
    skopeo copy --override-os linux --override-arch amd64 \
      docker://quay.io/argoproj/argo-rollouts:v1.7.2 \
      docker-archive:/var/lib/rancher/k3s/agent/images/argo-rollouts.tar:quay.io/argoproj/argo-rollouts:v1.7.2 && \
    curl -sLO https://github.com/argoproj/argo-rollouts/releases/download/v1.7.2/kubectl-argo-rollouts-linux-amd64 && \
    chmod +x kubectl-argo-rollouts-linux-amd64 && \
    mv kubectl-argo-rollouts-linux-amd64 /usr/local/bin/kubectl-argo-rollouts && \
    apt-get clean && rm -rf /var/lib/apt/lists/*
