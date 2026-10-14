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
WORKERS="${WORKERS:-1}"
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

echo "=== [deploy] 4. Configurando serviço systemd (poi-io.service) ==="
sudo tee /etc/systemd/system/poi-io.service > /dev/null << EOF
[Unit]
Description=POI IO-bound FastAPI Workload
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${APP_DIR}
Environment="IO_DIR=${IO_DIR}"
Environment="DEFAULT_SIZE_MB=${DEFAULT_SIZE_MB}"
Environment="DEFAULT_ITERATIONS=${DEFAULT_ITERATIONS}"
Environment="IO_CHUNK_KB=${IO_CHUNK_KB}"
ExecStart=${VENV_DIR}/bin/uvicorn app.main:app \
    --host 0.0.0.0 \
    --port ${APP_PORT} \
    --workers ${WORKERS} \
    --log-level info
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable poi-io
sudo systemctl restart poi-io

echo ""
echo "=== [deploy] 5. Validando disponibilidade do serviço ==="
sleep 2
if curl -sf "http://127.0.0.1:${APP_PORT}/health" > /dev/null; then
    echo "OK: aplicação respondendo em http://127.0.0.1:${APP_PORT} (IO_DIR=${IO_DIR})"
else
    echo "FALHA: verifique logs: sudo journalctl -u poi-io -n 40"
    exit 1
fi

echo ""
echo "Status do serviço:"
sudo systemctl status poi-io --no-pager
