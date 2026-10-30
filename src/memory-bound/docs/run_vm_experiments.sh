#!/usr/bin/env bash
# run_vm_experiments.sh — Executa os cenários de provisionamento na VM Ubuntu
#
# LOCAL DE EXECUÇÃO:
#   Este script é executado FORA da VM (na máquina host pessoal ou no LCC).
#   A geração de carga com k6 ocorre externamente na rede; as métricas de
#   infraestrutura da VM são coletadas internamente dentro do sistema convidado via SSH.
#
# Pré-requisitos:
#   - k6 instalado na máquina externa
#   - Conexão SSH sem senha (chave pública) para a VM Ubuntu
#   - Aplicação deployada e ativa na VM via docs/deploy_vm.sh
#
# Cenários de Provisionamento Propostos (VirtualBox):
#   - Cenário Referência (C1): 1 vCPU, 1 GiB RAM (configuração base fornecida pelo professor)
#   - Cenário C2: 1 vCPU, 2 GiB RAM (isolamento do efeito de expansão de RAM sob mesma CPU)
#   - Cenário C3: 2 vCPUs, 2 GiB RAM (efeito de paralelismo de CPU com memória moderada)
#   - Cenário C4: 2 vCPUs, 4 GiB RAM (provisionamento ampliado dentro dos limites do hospedeiro)
#   *NOTA*: Caso o professor exija manter vCPU/RAM estritamente fixos em 1 vCPU / 1 GiB,
#   execute exclusivamente o cenário C1 com diferentes níveis de demanda e consulte
#   o professor sobre a interpretação do requisito de múltiplos cenários.
#
# Uso:
#   VM_IP=192.168.56.10 SCENARIO=C1_1vcpu_1gib bash docs/run_vm_experiments.sh
#   VM_IP=192.168.56.10 SIZE_MIB=64 DWELL_MS=200 bash docs/run_vm_experiments.sh

set -euo pipefail

VM_IP="${VM_IP:-127.0.0.1}"
VM_USER="${VM_USER:-osboxes}"
VM_PORT="${VM_PORT:-2222}"
APP_PORT="${APP_PORT:-8000}"

# Acesso separado para SSH e HTTP: no VirtualBox ambos usam VM_IP; na nuvem o
# SSH e o HTTP passam pelo balanceador (VIP/portas podem ser diferentes).
SSH_HOST="${SSH_HOST:-${VM_IP}}"
SSH_PORT="${SSH_PORT:-${VM_PORT}}"
TARGET_URL="${TARGET_URL:-http://${VM_IP}:${APP_PORT}}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-/opt/poi-memory}"
REMOTE_PYTHON="${REMOTE_PYTHON:-${REMOTE_APP_DIR}/.venv/bin/python}"
REMOTE_RESULTS_DIR="${REMOTE_RESULTS_DIR:-/tmp}"

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
PROFILE="${PROFILE:-memory}"
# Deriva vCPUs/RAM do nome do cenário (ex.: C2_2vcpu_2gib) quando não informados.
VCPUS="${VCPUS:-$(sed -n 's/.*_\([0-9]\+\)vcpu.*/\1/p' <<<"${SCENARIO}")}"
VCPUS="${VCPUS:-1}"
RAM_GIB="${RAM_GIB:-$(sed -n 's/.*_\([0-9]\+\)gib.*/\1/p' <<<"${SCENARIO}")}"
RAM_GIB="${RAM_GIB:-1}"
# Multi-worker: por padrão, um worker por vCPU do cenário.
WORKERS="${WORKERS:-${VCPUS}}"
REQUEST_MIX="${REQUEST_MIX:-uniform}"
REP="${REP:-1}"
SIZE_MIB="${SIZE_MIB:-8}"       # buffer por requisição (folga sob MAX_CONCURRENT_MIB=384)
ITERATIONS="${ITERATIONS:-1}"
DWELL_MS="${DWELL_MS:-200}"     # retenção do buffer (é ela que empilha o RSS)
SERVICE="${SERVICE:-poi-app}"
DURATION="${DURATION:-45s}"

# Níveis de demanda conservadores para teste em 1 vCPU / 1 GiB RAM
VUS_LEVELS=(1 2 4 8 16)

echo "======================================================="
echo " Experimento VM — Cenário: ${SCENARIO}"
echo " Alvo Remoto: ${TARGET_URL}"
echo " Parâmetros: size_mib=${SIZE_MIB} | iterations=${ITERATIONS} | dwell_ms=${DWELL_MS}"
echo " Execução: workers=${WORKERS} | vCPUs=${VCPUS} | RAM=${RAM_GIB} GiB | mix=${REQUEST_MIX} | rep=${REP}"
echo " Níveis de demanda programados: ${VUS_LEVELS[*]} VUs"
echo "======================================================="

echo "[1/4] Testando conectividade com o endpoint /health da VM..."
if ! curl -sf --max-time 5 "${TARGET_URL}/health" > /dev/null; then
    echo "✘ ERRO: VM não acessível em ${TARGET_URL}. Verifique IP, rede e serviço."
    exit 1
fi
echo "✔ VM respondendo com sucesso."

for VUS in "${VUS_LEVELS[@]}"; do
    echo ""
    echo "--- Executando nível de demanda: ${VUS} VUs ---"
    TS=$(date -u +%Y%m%dT%H%M%SZ)
    RUN_ID="${PROFILE}_${SCENARIO}_w${WORKERS}_${REQUEST_MIX}_${VUS}vus_r${REP}_${TS}"

    # 0. Descobre o PID da aplicação na VM (necessário para proc_rss_mib no CSV)
    APP_PID=$(ssh -p "${SSH_PORT}" "${VM_USER}@${SSH_HOST}" \
        "systemctl show -p MainPID --value ${SERVICE}" 2>/dev/null || echo "")

    ssh -p "${SSH_PORT}" "${VM_USER}@${SSH_HOST}" "mkdir -p ${REMOTE_RESULTS_DIR}"

    # 1. Inicia o monitor de recursos DENTRO da VM via SSH
    SYS_CSV_LOCAL="${OUT_DIR}/sys_vm_${RUN_ID}.csv"
    echo "  [SSH] Iniciando system_monitor.py na VM (target-pid=${APP_PID:-n/a})..."
    VM_PID=$(ssh -p "${SSH_PORT}" "${VM_USER}@${SSH_HOST}" \
        "nohup ${REMOTE_PYTHON} ${REMOTE_APP_DIR}/metrics/collectors/system_monitor.py \
            --label ${RUN_ID} --run-id ${RUN_ID} --profile ${PROFILE} \
            --scenario ${SCENARIO} --vcpus ${VCPUS} --ram-gib ${RAM_GIB} \
            --workers ${WORKERS} --request-mix ${REQUEST_MIX} --rep ${REP} \
            --duration 120 --interval 1.0 --machine vm \
            ${APP_PID:+--target-pid ${APP_PID} --include-children} \
            --out-dir ${REMOTE_RESULTS_DIR} > ${REMOTE_RESULTS_DIR}/monitor_${RUN_ID}.log 2>&1 &
        echo \$!")
    echo "        PID do monitor na VM: ${VM_PID}"

    sleep 2 # Estabilização

    # 2. Executa geração de carga externa via k6
    echo "  [k6] Gerando carga externa (${VUS} VUs durante ${DURATION})..."
    "${K6_BIN}" run k6/load_ramp.js \
        -e "TARGET_URL=${TARGET_URL}" \
        -e "SIZE_MIB=${SIZE_MIB}" \
        -e "ITERATIONS=${ITERATIONS}" \
        -e "DWELL_MS=${DWELL_MS}" \
        -e "VUS=${VUS}" \
        -e "DURATION=${DURATION}" \
        -e "OUT_DIR=${OUT_DIR}" \
        -e "RUN_ID=${RUN_ID}" \
        -e "SCENARIO=${SCENARIO}" \
        -e "VCPUS=${VCPUS}" \
        -e "RAM_GIB=${RAM_GIB}" \
        -e "WORKERS=${WORKERS}" \
        -e "REQUEST_MIX=${REQUEST_MIX}" \
        -e "REP=${REP}" || true

    # 3. Encerra o monitor na VM e recupera o arquivo de métricas reais da VM
    echo "  [SSH] Encerrando monitor na VM e transferindo CSV..."
    ssh -p "${SSH_PORT}" "${VM_USER}@${SSH_HOST}" "kill ${VM_PID} 2>/dev/null || true" || true
    sleep 1

    REMOTE_CSV=$(ssh -p "${SSH_PORT}" "${VM_USER}@${SSH_HOST}" \
        "ls ${REMOTE_RESULTS_DIR}/sys_vm_${RUN_ID}_*.csv 2>/dev/null | head -1" || echo "")

    if [ -n "${REMOTE_CSV}" ]; then
        scp -P "${SSH_PORT}" "${VM_USER}@${SSH_HOST}:${REMOTE_CSV}" "${SYS_CSV_LOCAL}"
        echo "        ✔ CSV de infraestrutura da VM salvo em ${SYS_CSV_LOCAL}"
        ssh -p "${SSH_PORT}" "${VM_USER}@${SSH_HOST}" "rm -f ${REMOTE_CSV}" || true
    else
        echo "        [aviso] Arquivo de métricas da VM não foi localizado no /tmp remoto."
    fi

    echo "  Nível concluído: ${RUN_ID}"
    sleep 3
done

echo ""
echo "======================================================="
echo " Todos os níveis de demanda concluídos para ${SCENARIO}"
echo " Gere os gráficos comparativos executando:"
echo "   python plots/generate_plots.py"
echo "======================================================="
