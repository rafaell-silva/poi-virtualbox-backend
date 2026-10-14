#!/usr/bin/env bash
# run_vm_experiments.sh — Executa os níveis de demanda contra a VM (CPU-bound)
#
# LOCAL DE EXECUÇÃO:
#   Este script é executado FORA da VM (na máquina host pessoal ou no LCC).
#   A geração de carga com k6 ocorre externamente na rede; as métricas de
#   infraestrutura da VM são coletadas internamente (dentro do guest) via SSH.
#
# Pré-requisitos:
#   - k6 instalado na máquina externa
#   - Conexão SSH sem senha (chave pública) para a VM Ubuntu
#   - Aplicação deployada e ativa na VM via docs/deploy_vm.sh
#
# Cenários de Provisionamento (VirtualBox):
#   - C1: 1 vCPU, 1 GiB RAM (referência)
#   - C2: 2 vCPUs, 1 GiB RAM
#   - C3: 2 vCPUs, 2 GiB RAM
#   - C4: 4 vCPUs, 4 GiB RAM
#
# Uso:
#   VM_IP=192.168.56.10 SCENARIO=C1_1vcpu_1gib bash docs/run_vm_experiments.sh

set -euo pipefail

VM_IP="${VM_IP:-127.0.0.1}"
VM_USER="${VM_USER:-osboxes}"
VM_PORT="${VM_PORT:-2222}"
APP_PORT="${APP_PORT:-8000}"
TARGET_URL="http://${VM_IP}:${APP_PORT}"

# Detecta k6 no PATH ou no local padrão do Windows
if command -v k6 >/dev/null 2>&1; then
    K6_BIN="k6"
elif [ -f "/c/Program Files/k6/k6.exe" ]; then
    K6_BIN="/c/Program Files/k6/k6.exe"
elif [ -f "C:/Program Files/k6/k6.exe" ]; then
    K6_BIN="C:/Program Files/k6/k6.exe"
else
    echo "ERRO: k6 não encontrado no PATH nem em 'C:\\Program Files\\k6\\k6.exe'."
    exit 1
fi

OUT_DIR="data/results"
mkdir -p "${OUT_DIR}"

SCENARIO="${SCENARIO:-C1_1vcpu_1gib}"
INTENSITY="${INTENSITY:-1000}"
CHUNK_KIB="${CHUNK_KIB:-64}"
DURATION="${DURATION:-45s}"
SERVICE="${SERVICE:-poi-cpu}"

# Níveis de demanda conservadores para 1 vCPU / 1 GiB RAM
VUS_LEVELS=(1 2 4 8 16)

echo "======================================================="
echo " Experimento VM (CPU-bound) — Cenário: ${SCENARIO}"
echo " Alvo Remoto: ${TARGET_URL}"
echo " Parâmetros: INTENSITY=${INTENSITY} | CHUNK_KIB=${CHUNK_KIB}"
echo " Níveis de demanda programados: ${VUS_LEVELS[*]} VUs"
echo "======================================================="

echo "[1/4] Testando conectividade com o endpoint /health da VM..."
if ! curl -sf --max-time 5 "${TARGET_URL}/health" > /dev/null; then
    echo "ERRO: VM não acessível em ${TARGET_URL}. Verifique IP, rede e serviço."
    exit 1
fi
echo "OK: VM respondendo."

for VUS in "${VUS_LEVELS[@]}"; do
    echo ""
    echo "--- Executando nível de demanda: ${VUS} VUs ---"
    TS=$(date -u +%Y%m%dT%H%M%SZ)
    RUN_ID="${SCENARIO}_${INTENSITY}int_${CHUNK_KIB}ki_${VUS}vus_${TS}"

    # 0. Descobre o PID da aplicação na VM (para métricas de processo por núcleo)
    APP_PID=$(ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" \
        "systemctl show -p MainPID --value ${SERVICE}" 2>/dev/null || echo "")

    # 1. Inicia o monitor de recursos DENTRO da VM via SSH
    echo "  [SSH] Iniciando system_monitor.py na VM (target-pid=${APP_PID:-n/a})..."
    VM_PID=$(ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" \
        "nohup /opt/poi-cpu/.venv/bin/python /opt/poi-cpu/metrics/collectors/system_monitor.py \
            --label ${RUN_ID} --duration 180 --interval 1.0 --machine vm \
            ${APP_PID:+--target-pid ${APP_PID} --include-children} \
            --out-dir /tmp > /tmp/monitor_${RUN_ID}.log 2>&1 &
        echo \$!")
    echo "        PID do monitor na VM: ${VM_PID}"
    sleep 2

    # 2. Gera a carga EXTERNAMENTE com k6
    echo "  [k6] Gerando carga externa (${VUS} VUs durante ${DURATION})..."
    "${K6_BIN}" run k6/load-test.js \
        -e "BASE_URL=${TARGET_URL}" \
        -e "INTENSITY=${INTENSITY}" \
        -e "CHUNK_KIB=${CHUNK_KIB}" \
        -e "VUS=${VUS}" \
        -e "DURATION=${DURATION}" \
        -e "OUT_DIR=${OUT_DIR}" || true

    # 3. Encerra o monitor na VM e recupera o CSV de métricas reais
    echo "  [SSH] Encerrando monitor na VM e transferindo CSV..."
    ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" "kill ${VM_PID} 2>/dev/null || true" || true
    sleep 1

    REMOTE_CSV=$(ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" \
        "ls /tmp/sys_vm_${RUN_ID}_*.csv 2>/dev/null | head -1" || echo "")
    if [ -n "${REMOTE_CSV}" ]; then
        scp -P "${VM_PORT}" "${VM_USER}@${VM_IP}:${REMOTE_CSV}" \
            "${OUT_DIR}/sys_vm_${RUN_ID}.csv"
        echo "        OK: CSV de infraestrutura da VM salvo."
        ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" "rm -f ${REMOTE_CSV}" || true
    else
        echo "        [aviso] CSV de métricas da VM não localizado em /tmp remoto."
    fi

    echo "  Nível concluído: ${RUN_ID}"
    sleep 3
done

echo ""
echo "======================================================="
echo " Todos os níveis concluídos para ${SCENARIO}"
echo " Gere os gráficos com:"
echo "   python plots/generate_plots.py"
echo "======================================================="
