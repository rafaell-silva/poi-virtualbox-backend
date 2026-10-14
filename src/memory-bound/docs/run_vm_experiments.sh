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

VM_IP="${VM_IP:-192.168.56.10}"
VM_USER="${VM_USER:-ubuntu}"
VM_PORT="${VM_PORT:-22}"
APP_PORT="${APP_PORT:-8000}"
TARGET_URL="http://${VM_IP}:${APP_PORT}"

OUT_DIR="data/results"
mkdir -p "${OUT_DIR}"

SCENARIO="${SCENARIO:-C1_1vcpu_1gib}"
SIZE_MIB="${SIZE_MIB:-8}"       # buffer por requisição (folga sob MAX_CONCURRENT_MIB=384)
ITERATIONS="${ITERATIONS:-1}"
DWELL_MS="${DWELL_MS:-200}"     # retenção do buffer (é ela que empilha o RSS)
SERVICE="${SERVICE:-poi-app}"
DURATION="${DURATION:-45s}"

# Níveis de demanda conservadores para teste em 1 vCPU / 1 GiB RAM
VUS_LEVELS=(1 2 4 8 12)

echo "======================================================="
echo " Experimento VM — Cenário: ${SCENARIO}"
echo " Alvo Remoto: ${TARGET_URL}"
echo " Parâmetros: size_mib=${SIZE_MIB} | iterations=${ITERATIONS} | dwell_ms=${DWELL_MS}"
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
    RUN_ID="${SCENARIO}_${SIZE_MIB}mib_${DWELL_MS}ms_${VUS}vus_${TS}"

    # 0. Descobre o PID da aplicação na VM (necessário para proc_rss_mib no CSV)
    APP_PID=$(ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" \
        "systemctl show -p MainPID --value ${SERVICE}" 2>/dev/null || echo "")

    # 1. Inicia o monitor de recursos DENTRO da VM via SSH
    SYS_CSV_LOCAL="${OUT_DIR}/sys_vm_${RUN_ID}.csv"
    echo "  [SSH] Iniciando system_monitor.py na VM (target-pid=${APP_PID:-n/a})..."
    VM_PID=$(ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" \
        "nohup python3 /opt/poi-app/metrics/collectors/system_monitor.py \
            --label ${RUN_ID} --duration 120 --interval 1.0 --machine vm \
            ${APP_PID:+--target-pid ${APP_PID} --include-children} \
            --out-dir /tmp > /tmp/monitor_${RUN_ID}.log 2>&1 &
        echo \$!")
    echo "        PID do monitor na VM: ${VM_PID}"

    sleep 2 # Estabilização

    # 2. Executa geração de carga externa via k6
    echo "  [k6] Gerando carga externa (${VUS} VUs durante ${DURATION})..."
    k6 run k6/load_ramp.js \
        -e "TARGET_URL=${TARGET_URL}" \
        -e "SIZE_MIB=${SIZE_MIB}" \
        -e "ITERATIONS=${ITERATIONS}" \
        -e "DWELL_MS=${DWELL_MS}" \
        -e "VUS=${VUS}" \
        -e "DURATION=${DURATION}" \
        -e "OUT_DIR=${OUT_DIR}" || true

    # 3. Encerra o monitor na VM e recupera o arquivo de métricas reais da VM
    echo "  [SSH] Encerrando monitor na VM e transferindo CSV..."
    ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" "kill ${VM_PID} 2>/dev/null || true" || true
    sleep 1

    REMOTE_CSV=$(ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" \
        "ls /tmp/sys_vm_${RUN_ID}_*.csv 2>/dev/null | head -1" || echo "")

    if [ -n "${REMOTE_CSV}" ]; then
        scp -P "${VM_PORT}" "${VM_USER}@${VM_IP}:${REMOTE_CSV}" "${SYS_CSV_LOCAL}"
        echo "        ✔ CSV de infraestrutura da VM salvo em ${SYS_CSV_LOCAL}"
        ssh -p "${VM_PORT}" "${VM_USER}@${VM_IP}" "rm -f ${REMOTE_CSV}" || true
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
