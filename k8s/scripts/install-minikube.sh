#!/usr/bin/env bash
set -euo pipefail

# Instala Docker, kubectl e Minikube em Ubuntu Server 22.04 / 24.04 LTS.
# Script idempotente: etapas já instaladas são puladas.

echo "Atualizando pacotes base e instalando Docker Engine"
sudo apt-get update -q
sudo apt-get install -y curl ca-certificates apt-transport-https conntrack docker.io

sudo systemctl enable --now docker

if ! id -nG "$USER" | grep -qw docker; then
    sudo usermod -aG docker "$USER"
    ADDED_TO_DOCKER_GROUP=1
else
    ADDED_TO_DOCKER_GROUP=0
fi

echo "Instalando kubectl"
if ! command -v kubectl >/dev/null 2>&1; then
    KUBECTL_VERSION="$(curl -fsSL https://dl.k8s.io/release/stable.txt)"
    curl -fsSL -o /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
    sudo install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl
    rm -f /tmp/kubectl
else
    echo "kubectl já instalado em $(command -v kubectl)"
fi

echo "Instalando Minikube"
if ! command -v minikube >/dev/null 2>&1; then
    curl -fsSL -o /tmp/minikube-linux-amd64 https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
    sudo install -o root -g root -m 0755 /tmp/minikube-linux-amd64 /usr/local/bin/minikube
    rm -f /tmp/minikube-linux-amd64
else
    echo "minikube já instalado em $(command -v minikube)"
fi

echo "Versões instaladas"
docker --version
kubectl version --client
minikube version

if [ "$ADDED_TO_DOCKER_GROUP" -eq 1 ]; then
    echo ""
    echo "ATENÇÃO: o usuário '$USER' foi adicionado ao grupo 'docker'."
    echo "Execute 'newgrp docker' (ou reconecte sua sessão SSH) antes de rodar:"
    echo "  minikube start --driver=docker --cpus=3 --memory=6144 --addons=metrics-server"
fi
