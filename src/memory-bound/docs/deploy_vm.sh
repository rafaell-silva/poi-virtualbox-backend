#!/usr/bin/env bash
# deploy_vm.sh — Configura e inicia a aplicação na VM Ubuntu Server
#
# Execute este script DENTRO da VM (via SSH):
#   ssh user@<VM_IP>
#   bash deploy_vm.sh
#
# Restrição de Referência: VM com 1 vCPU e 1 GiB de RAM.
#
# O script:
#   1. Instala dependências do sistema operacional
#   2. Prepara o diretório da aplicação e sincroniza código
#   3. Cria o ambiente virtual Python (.venv)
#   4. Configura e ativa o serviço systemd com limites conservadores de segurança

set -euo pipefail

APP_PORT="${APP_PORT:-8000}"
MAX_SIZE_MIB="${MAX_SIZE_MIB:-128}"          # Limite individual conservador para 1 GiB RAM
MAX_CONCURRENT_MIB="${MAX_CONCURRENT_MIB:-256}" # Limite agregado para preservar RAM do SO
DEFAULT_SIZE_MIB="${DEFAULT_SIZE_MIB:-8}"
DEFAULT_ITERATIONS="${DEFAULT_ITERATIONS:-1}"
SERVICE_NAME="${SERVICE_NAME:-poi-app}"
WORKERS="${WORKERS:-$(nproc 2>/dev/null || echo 1)}" # multi-worker por padrão
HOST="${HOST:-0.0.0.0}"
LOG_DIR="${LOG_DIR:-}"                       # ex.: /mnt/poi-data/logs
DATA_MOUNT="${DATA_MOUNT:-}"                 # ex.: /mnt/poi-data
PROXY_HEADERS="${PROXY_HEADERS:-1}"          # atrás de nginx/LB
APP_DIR="${APP_DIR:-/opt/poi-app}"
VENV_DIR="${APP_DIR}/.venv"

echo "=== [deploy] 1. Instalando pacotes básicos do Ubuntu ==="
sudo apt-get update -q
sudo apt-get install -y -q python3 python3-pip python3-venv git rsync curl

echo "=== [deploy] 2. Preparando diretório ${APP_DIR} ==="
sudo mkdir -p "${APP_DIR}"
sudo chown "${USER}:${USER}" "${APP_DIR}"

if [ -n "${REPO_URL:-}" ]; then
    echo "=== [deploy] Clonando repositório ${REPO_URL} ==="
    git -C "${APP_DIR}" pull 2>/dev/null || git clone "${REPO_URL}" "${APP_DIR}"
else
    echo "=== [deploy] Sincronizando arquivos locais para ${APP_DIR} ==="
    rsync -av --exclude='.git' --exclude='.venv' --exclude='data/results/' \
        . "${APP_DIR}/"
fi

echo "=== [deploy] 3. Configurando virtualenv em ${VENV_DIR} ==="
python3 -m venv "${VENV_DIR}"
"${VENV_DIR}/bin/pip" install --upgrade pip --quiet
"${VENV_DIR}/bin/pip" install fastapi uvicorn psutil --quiet

LOG_CONFIG=""
if [ -n "${LOG_DIR}" ]; then
    sudo mkdir -p "${LOG_DIR}"
    sudo chown "${USER}:${USER}" "${LOG_DIR}"
    LOG_CONFIG="StandardOutput=append:${LOG_DIR}/${SERVICE_NAME}.log
StandardError=append:${LOG_DIR}/${SERVICE_NAME}.log"
fi

MOUNT_CONFIG=""
if [ -n "${DATA_MOUNT}" ]; then
    MOUNT_CONFIG="RequiresMountsFor=${DATA_MOUNT}"
fi

PROXY_ARGS=""
if [ "${PROXY_HEADERS}" = "1" ]; then
    PROXY_ARGS="--proxy-headers --forwarded-allow-ips=127.0.0.1"
fi

echo "=== [deploy] 4. Configurando serviço systemd (${SERVICE_NAME}.service) ==="
sudo tee "/etc/systemd/system/${SERVICE_NAME}.service" > /dev/null << EOF
[Unit]
Description=POI Memory-bound FastAPI Workload
After=network.target
${MOUNT_CONFIG}

[Service]
Type=simple
User=${USER}
WorkingDirectory=${APP_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="MAX_SIZE_MIB=${MAX_SIZE_MIB}"
Environment="MAX_CONCURRENT_MIB=${MAX_CONCURRENT_MIB}"
Environment="DEFAULT_SIZE_MIB=${DEFAULT_SIZE_MIB}"
Environment="DEFAULT_ITERATIONS=${DEFAULT_ITERATIONS}"
ExecStart=${VENV_DIR}/bin/uvicorn app.main:app \
    --host ${HOST} \
    --port ${APP_PORT} \
    --workers ${WORKERS} \
    --log-level info ${PROXY_ARGS}
${LOG_CONFIG}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"
sudo systemctl restart "${SERVICE_NAME}"

echo ""
echo "=== [deploy] 5. Validando disponibilidade do serviço ==="
sleep 2
if curl -sf "http://127.0.0.1:${APP_PORT}/health" > /dev/null; then
    echo "✔ Aplicação respondendo perfeitamente em http://127.0.0.1:${APP_PORT}"
else
    echo "✘ Falha ao responder. Verifique logs: sudo journalctl -u ${SERVICE_NAME} -n 40"
    exit 1
fi

echo ""
echo "Status do serviço:"
sudo systemctl status "${SERVICE_NAME}" --no-pager
