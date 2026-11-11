#!/usr/bin/env bash
# deploy_vm.sh — Configura e inicia a aplicação IO-bound na VM Ubuntu Server
#
# Execute este script DENTRO da VM (via SSH):
#   ssh user@<VM_IP>
#   sudo mkdir -p /data/io && sudo chown "$USER" /data/io
#   IO_DIR=/data/io bash deploy_vm.sh
#
# Restrição de referência: VM com 1 vCPU e 1 GiB de RAM.
#
# IMPORTANTE — Disco real:
#   IO_DIR DEVE apontar para um volume em disco real (ex.: /data/io no disco do
#   VirtualBox). Se apontar para tmpfs (/dev/shm ou /tmp em tmpfs), a carga deixa
#   de ser IO-bound.
#
# O script:
#   1. Instala dependências do sistema operacional
#   2. Prepara o diretório da aplicação e o diretório de I/O
#   3. Cria o ambiente virtual Python (.venv)
#   4. Configura e ativa o serviço systemd (poi-io.service)

set -euo pipefail

APP_PORT="${APP_PORT:-8000}"
IO_DIR="${IO_DIR:-/data/io}"
DEFAULT_SIZE_MB="${DEFAULT_SIZE_MB:-8}"
DEFAULT_ITERATIONS="${DEFAULT_ITERATIONS:-1}"
IO_CHUNK_KB="${IO_CHUNK_KB:-1024}"
SERVICE_NAME="${SERVICE_NAME:-poi-io}"
WORKERS="${WORKERS:-$(nproc 2>/dev/null || echo 1)}" # multi-worker por padrão
HOST="${HOST:-0.0.0.0}"
LOG_DIR="${LOG_DIR:-}"                       # ex.: /mnt/poi-data/logs
DATA_MOUNT="${DATA_MOUNT:-}"                 # ex.: /mnt/poi-data
PROXY_HEADERS="${PROXY_HEADERS:-1}"          # atrás de nginx/LB
APP_DIR="${APP_DIR:-/opt/poi-io}"
VENV_DIR="${APP_DIR}/.venv"

echo "=== [deploy] 1. Instalando pacotes básicos do Ubuntu ==="
sudo apt-get update -q
sudo apt-get install -y -q python3 python3-pip python3-venv git rsync curl

echo "=== [deploy] 2. Preparando diretório ${APP_DIR} e volume de I/O ${IO_DIR} ==="
sudo mkdir -p "${APP_DIR}" "${IO_DIR}"
sudo chown "${USER}:${USER}" "${APP_DIR}" "${IO_DIR}"

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
Description=POI IO-bound FastAPI Workload
After=network.target
${MOUNT_CONFIG}

[Service]
Type=simple
User=${USER}
WorkingDirectory=${APP_DIR}
Environment="PYTHONUNBUFFERED=1"
Environment="IO_DIR=${IO_DIR}"
Environment="DEFAULT_SIZE_MB=${DEFAULT_SIZE_MB}"
Environment="DEFAULT_ITERATIONS=${DEFAULT_ITERATIONS}"
Environment="IO_CHUNK_KB=${IO_CHUNK_KB}"
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
    echo "OK: aplicação respondendo em http://127.0.0.1:${APP_PORT} (IO_DIR=${IO_DIR})"
else
    echo "FALHA: verifique logs: sudo journalctl -u ${SERVICE_NAME} -n 40"
    exit 1
fi

echo ""
echo "Status do serviço:"
sudo systemctl status "${SERVICE_NAME}" --no-pager
