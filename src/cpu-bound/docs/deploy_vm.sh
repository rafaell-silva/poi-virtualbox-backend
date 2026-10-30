#!/usr/bin/env bash
# deploy_vm.sh — Configura e inicia a aplicação CPU-bound na VM Ubuntu Server
#
# Execute este script DENTRO da VM (via SSH):
#   ssh user@<VM_IP>
#   bash deploy_vm.sh
#
# Restrição de referência: VM com 1 vCPU e 1 GiB de RAM.
#
# O script:
#   1. Instala dependências do sistema operacional
#   2. Prepara o diretório da aplicação e sincroniza o código
#   3. Cria o ambiente virtual Python (.venv)
#   4. Configura e ativa o serviço systemd (poi-cpu.service)

set -euo pipefail

APP_PORT="${APP_PORT:-8000}"
DEFAULT_INTENSITY="${DEFAULT_INTENSITY:-1000}" # iterações de hash por requisição
DEFAULT_CHUNK_KIB="${DEFAULT_CHUNK_KIB:-64}"   # bloco hasheado por iteração (>=2 libera o GIL)
WORKERS="${WORKERS:-1}"                          # 1 worker para baseline de 1 vCPU
APP_DIR="${APP_DIR:-/opt/poi-cpu}"
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
"${VENV_DIR}/bin/pip" install fastapi "uvicorn[standard]" psutil --quiet

echo "=== [deploy] 4. Configurando serviço systemd (poi-cpu.service) ==="
sudo tee /etc/systemd/system/poi-cpu.service > /dev/null << EOF
[Unit]
Description=POI CPU-bound FastAPI Workload
After=network.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${APP_DIR}
Environment="DEFAULT_INTENSITY=${DEFAULT_INTENSITY}"
Environment="DEFAULT_CHUNK_KIB=${DEFAULT_CHUNK_KIB}"
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
sudo systemctl enable poi-cpu
sudo systemctl restart poi-cpu

echo ""
echo "=== [deploy] 5. Validando disponibilidade do serviço ==="
sleep 2
if curl -sf "http://127.0.0.1:${APP_PORT}/health" > /dev/null; then
    echo "OK: aplicação respondendo em http://127.0.0.1:${APP_PORT}"
else
    echo "FALHA: verifique logs: sudo journalctl -u poi-cpu -n 40"
    exit 1
fi

echo ""
echo "Status do serviço:"
sudo systemctl status poi-cpu --no-pager
