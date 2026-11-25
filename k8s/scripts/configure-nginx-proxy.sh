#!/usr/bin/env bash
set -euo pipefail

# Configura o Nginx na VM para encaminhar /cpu/, /memory/ e /io/ para os
# NodePorts (30080, 30081, 30082) expostos pelo Minikube.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_PATH="${SCRIPT_DIR}/../nginx/nginx-poi-k8s.conf.template"

if [ ! -f "$TEMPLATE_PATH" ]; then
    echo "ERRO: template não encontrado em $TEMPLATE_PATH" >&2
    exit 1
fi

if ! command -v minikube >/dev/null 2>&1; then
    echo "ERRO: minikube não encontrado no PATH." >&2
    exit 1
fi

MINIKUBE_IP="$(minikube ip)"
if [ -z "$MINIKUBE_IP" ]; then
    echo "ERRO: não foi possível obter o IP do Minikube. Verifique 'minikube status'." >&2
    exit 1
fi

echo "IP do Minikube detectado: ${MINIKUBE_IP}"

if ! command -v nginx >/dev/null 2>&1; then
    echo "Instalando nginx..."
    sudo apt-get update -q
    sudo apt-get install -y nginx
fi

TMP_CONF="$(mktemp)"
trap 'rm -f "$TMP_CONF"' EXIT

sed "s/__MINIKUBE_IP__/${MINIKUBE_IP}/g" "$TEMPLATE_PATH" > "$TMP_CONF"

sudo cp "$TMP_CONF" /etc/nginx/sites-available/poi-k8s
sudo ln -sf /etc/nginx/sites-available/poi-k8s /etc/nginx/sites-enabled/poi-k8s
sudo rm -f /etc/nginx/sites-enabled/default

sudo nginx -t
sudo systemctl enable --now nginx
sudo systemctl reload nginx

echo ""
echo "Nginx configurado e recarregado com sucesso!"
echo "Teste localmente na VM com:"
echo "  curl -s http://127.0.0.1/health"
echo "  curl -s http://127.0.0.1/cpu/health && curl -s http://127.0.0.1/cpu/info"
echo "  curl -s http://127.0.0.1/memory/health && curl -s http://127.0.0.1/memory/info"
echo "  curl -s http://127.0.0.1/io/health && curl -s http://127.0.0.1/io/info"
