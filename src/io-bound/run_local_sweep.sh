#!/usr/bin/env bash
# run_local_sweep.sh — Executa uma varredura coordenada de níveis de demanda
# com um único servidor uvicorn ativo e monitor de recursos em background.
#
# Uso:
#   bash run_local_sweep.sh
#   SIZE_MIB=8 ITERATIONS=1 VUS_LEVELS="1 2 4 8 16 32" bash run_local_sweep.sh
#
# Varredura local: escada larga (1..32 VUs) para evidenciar a saturação.
# Para a VM de 1 vCPU / 1 GiB, use VUS_LEVELS="1 2 4 8 12".

set -euo pipefail

SIZE_MIB="${SIZE_MIB:-${SIZE_MB:-8}}"
ITERATIONS="${ITERATIONS:-1}"
WORKERS="${WORKERS:-1}" # 1 worker como baseline para VM de 1 vCPU
PORT="${PORT:-8000}"
DURATION="${DURATION:-20}" # 20s por nível de demanda
VUS_LEVELS=(${VUS_LEVELS:-1 2 4 8 16 32})

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_UV="${ROOT_DIR}/.venv/bin/uvicorn"
VENV_PY="${ROOT_DIR}/.venv/bin/python"
DATA_DIR="${ROOT_DIR}/data/results"
PLOTS_DIR="${ROOT_DIR}/plots/output"
mkdir -p "${DATA_DIR}" "${PLOTS_DIR}"

TS_START=$(date -u +%Y%m%dT%H%M%SZ)
SWEEP_RUN_ID="sweep_${SIZE_MIB}m"

# Função para liberar a porta antes de iniciar
kill_port() {
    lsof -ti tcp:"${PORT}" 2>/dev/null | xargs -r kill -9 2>/dev/null || true
    sleep 0.5
    if lsof -ti tcp:"${PORT}" >/dev/null 2>&1; then
        echo "ERRO: porta ${PORT} ainda ocupada por outro processo (lsof -i tcp:${PORT})."
        exit 1
    fi
}

echo "=========================================================="
echo " Execução Coordenada de Varredura Local (Sweep)"
echo " Identificador da Execução: ${SWEEP_RUN_ID} (${TS_START})"
echo " Parâmetros: ${SIZE_MIB} MiB | ${ITERATIONS} iterações | ${WORKERS} worker(s)"
echo " Níveis de Demanda: ${VUS_LEVELS[*]} VUs (${DURATION}s cada)"
echo "=========================================================="

echo "[1/4] Iniciando servidor uvicorn (Porta: ${PORT})..."
kill_port
${VENV_UV} app.main:app --host 127.0.0.1 --port "${PORT}" \
    --workers "${WORKERS}" > /tmp/sweep_uvicorn.log 2>&1 &
SERVER_PID=$!
echo "      Uvicorn ativo com PID=${SERVER_PID}"

# Aguarda servidor responder /health
SERVER_READY=0
for i in $(seq 1 30); do
    if curl -sf "http://127.0.0.1:${PORT}/health" > /dev/null 2>&1; then
        SERVER_READY=1
        echo "      Servidor respondendo OK."
        break
    fi
    sleep 0.5
done

if [ "${SERVER_READY}" -ne 1 ] || ! kill -0 "${SERVER_PID}" 2>/dev/null; then
    echo "ERRO: o servidor uvicorn (PID ${SERVER_PID}) não iniciou/respondeu corretamente."
    cat /tmp/sweep_uvicorn.log
    kill "${SERVER_PID}" 2>/dev/null || true
    exit 1
fi

echo "[2/4] Iniciando coletor de métricas do sistema com ID: ${SWEEP_RUN_ID}_all..."
${VENV_PY} metrics/collectors/system_monitor.py \
    --label "${SWEEP_RUN_ID}_all" \
    --target-pid "${SERVER_PID}" \
    --include-children \
    --interval 1.0 \
    --machine local \
    --out-dir "${DATA_DIR}" > /tmp/sweep_monitor.log 2>&1 &
MONITOR_PID=$!
echo "      Monitor ativo com PID=${MONITOR_PID}"

# Função de limpeza automática
cleanup() {
    echo ""
    echo "[Cleanup] Encerrando servidor e monitor de métricas..."
    kill "${SERVER_PID}" 2>/dev/null || true
    kill "${MONITOR_PID}" 2>/dev/null || true
    wait "${SERVER_PID}" 2>/dev/null || true
    wait "${MONITOR_PID}" 2>/dev/null || true
}
trap cleanup EXIT

echo ""
echo "[3/4] Executando níveis de demanda..."
for VUS in "${VUS_LEVELS[@]}"; do
    echo "  → Testando ${VUS} VUs (${DURATION}s)..."
    LABEL="${SWEEP_RUN_ID}_${VUS}vus"
    ${VENV_PY} metrics/collectors/run_experiment.py \
        --target-url "http://127.0.0.1:${PORT}" \
        --size-mib "${SIZE_MIB}" \
        --iterations "${ITERATIONS}" \
        --vus "${VUS}" \
        --duration "${DURATION}" \
        --label "${LABEL}" \
        --out-dir "${DATA_DIR}"
    sleep 2
done

echo ""
echo "[4/4] Gerando gráficos atualizados a partir das medições reais..."
${VENV_PY} plots/generate_plots.py \
    --results-dir "${DATA_DIR}" \
    --out-dir "${PLOTS_DIR}"

echo ""
echo "=========================================================="
echo " Varredura local concluída com sucesso!"
echo " Dados gravados em: ${DATA_DIR}/"
echo " Gráficos atualizados em: ${PLOTS_DIR}/"
echo "=========================================================="
