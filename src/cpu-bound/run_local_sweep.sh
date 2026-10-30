#!/usr/bin/env bash
# run_local_sweep.sh — Executa uma varredura coordenada de níveis de demanda
# com um único servidor uvicorn ativo e monitor de recursos em background.
#
# Uso:
#   bash run_local_sweep.sh
#   INTENSITY=2000 CHUNK_KIB=64 VUS_LEVELS="1 2 4 8 16 32" bash run_local_sweep.sh
#   ITERATIONS=2 bash run_local_sweep.sh      # atalho: INTENSITY = ITERATIONS * 1000
#
# Varredura local: escada larga (1..32 VUs) para evidenciar a saturação.
# Para a VM de 1 vCPU / 1 GiB, use VUS_LEVELS="1 2 4 8 12".
#
# IMPORTANTE sobre métricas de processo:
#   O servidor é iniciado aqui e seu PID é repassado a run_experiment.py via
#   --target-pid (com --include-children). Sem isso, o CSV de cada nível de
#   demanda não teria proc_rss_mib/proc_cpu_percent e o gráfico
#   Demanda × Recursos cairia no fallback de CPU/RAM do sistema inteiro
#   (média diluída entre todos os núcleos), o que deixa o perfil da
#   aplicação invisível nos gráficos.

set -euo pipefail

INTENSITY="${INTENSITY:-$(( ${ITERATIONS:-1} * 1000 ))}" # iterações de hash por requisição
CHUNK_KIB="${CHUNK_KIB:-64}" # bloco hasheado (>=2 KiB libera o GIL)
WORKERS="${WORKERS:-1}" # 1 worker como baseline para VM de 1 vCPU
PORT="${PORT:-8000}"
DURATION="${DURATION:-20}" # 20s por nível de demanda
VUS_LEVELS=(${VUS_LEVELS:-1 2 4 8 16 32})

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -d "${ROOT_DIR}/.venv/Scripts" ]; then
    VENV_UV="${ROOT_DIR}/.venv/Scripts/uvicorn"
    VENV_PY="${ROOT_DIR}/.venv/Scripts/python"
else
    VENV_UV="${ROOT_DIR}/.venv/bin/uvicorn"
    VENV_PY="${ROOT_DIR}/.venv/bin/python"
fi
DATA_DIR="${ROOT_DIR}/data/results"
PLOTS_DIR="${ROOT_DIR}/plots/output"
mkdir -p "${DATA_DIR}" "${PLOTS_DIR}"

TS_START=$(date -u +%Y%m%dT%H%M%SZ)
SWEEP_RUN_ID="sweep_${INTENSITY}i"

# Porta ocupada por algo que o lsof do host NÃO enxerga (ex.: container Docker
# com porta publicada). Detecta via connect TCP e aborta com instrução acionável.
port_in_use() {
    "${VENV_PY}" -c '
import socket, sys
s = socket.socket()
s.settimeout(1.0)
try:
    sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
finally:
    s.close()
' "$1"
}

# Função para liberar a porta antes de iniciar
kill_port() {
    "${VENV_PY}" -c '
import socket, sys, psutil
port = int(sys.argv[1])
for proc in psutil.process_iter(["pid", "connections"]):
    try:
        for c in (proc.info.get("connections") or []):
            if c.laddr and c.laddr.port == port:
                proc.kill()
    except Exception:
        pass
' "${PORT}" 2>/dev/null || true
    sleep 0.5
    if port_in_use "${PORT}"; then
        echo "ERRO: a porta ${PORT} já está em uso e não pertence a um processo do host."
        echo "      (o lsof não a enxerga — típico de container Docker com porta publicada)"
        if command -v docker >/dev/null 2>&1; then
            docker ps --filter "publish=${PORT}" --format '      → {{.Names}} ({{.Image}})' 2>/dev/null
            echo "      Solução: encerre o container, ex.:  docker compose down"
            echo "      ou rode com outra porta:  PORT=8100 ./run_local_sweep.sh"
        fi
        exit 1
    fi
}

# Valida que o uvicorn de fato subiu e é o processo que vamos monitorar.
# Sem isso, um servidor órfão de execução anterior pode responder ao /health
# enquanto o PID capturado por $! está morto — e as métricas de processo saem
# zeradas, produzindo gráficos de CPU/RAM falsamente planos.
validate_server() {
    local pid="$1"
    # O uvicorn pode levar ~1s para gravar o erro de bind no log. Dá um tempo
    # para o log assentar antes de inspecionar, senão a corrida faz o sweep
    # prosseguir com um PID morto e as métricas de processo saem zeradas.
    for _ in 1 2 3 4 5 6; do
        [ -s /tmp/sweep_uvicorn.log ] && break
        sleep 0.3
    done
    sleep 0.7
    if grep -qiE "address already in use|error while attempting to bind" /tmp/sweep_uvicorn.log; then
        echo "ERRO: o uvicorn não conseguiu prender a porta ${PORT} (servidor órfão ativo)."
        echo "      Log: /tmp/sweep_uvicorn.log"
        cat /tmp/sweep_uvicorn.log
        kill "${pid}" 2>/dev/null || true
        exit 1
    fi
    local logged_pid
    logged_pid=$(sed -n 's/.*Started server process \[\([0-9]*\)\].*/\1/p' /tmp/sweep_uvicorn.log | head -1)
    if [ -n "${logged_pid}" ] && [ "${logged_pid}" != "${pid}" ]; then
        echo "      Nota: Ajustando SERVER_PID de ${pid} para PID nativo ${logged_pid}."
        SERVER_PID="${logged_pid}"
    fi
    if ! "${VENV_PY}" -c '
import sys, psutil
pid = int(sys.argv[1])
try:
    p = psutil.Process(pid)
except psutil.NoSuchProcess:
    sys.exit(1)
if p.status() == psutil.STATUS_ZOMBIE:
    sys.exit(1)
sys.exit(0 if p.memory_info().rss > 0 else 1)
' "${SERVER_PID}"; then
        echo "ERRO: o PID ${SERVER_PID} não é um processo vivo com RSS > 0."
        echo "      Log: /tmp/sweep_uvicorn.log"
        cat /tmp/sweep_uvicorn.log
        kill "${pid}" 2>/dev/null || true
        kill "${SERVER_PID}" 2>/dev/null || true
        exit 1
    fi
}

echo "=========================================================="
echo " Execução Coordenada de Varredura Local (Sweep)"
echo " Identificador da Execução: ${SWEEP_RUN_ID} (${TS_START})"
echo " Parâmetros: intensity=${INTENSITY} | chunk=${CHUNK_KIB} KiB | ${WORKERS} worker(s)"
echo " Níveis de Demanda: ${VUS_LEVELS[*]} VUs (${DURATION}s cada)"
echo "=========================================================="

echo "[1/4] Iniciando servidor uvicorn (Porta: ${PORT})..."
kill_port
${VENV_UV} app.main:app --host 127.0.0.1 --port "${PORT}" \
    --workers "${WORKERS}" > /tmp/sweep_uvicorn.log 2>&1 &
SHELL_PID=$!
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

if [ "${SERVER_READY}" -ne 1 ]; then
    echo "ERRO: o servidor uvicorn não iniciou/respondeu corretamente."
    cat /tmp/sweep_uvicorn.log
    kill "${SERVER_PID}" 2>/dev/null || true
    exit 1
fi
validate_server "${SERVER_PID}"

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
    [ "${SHELL_PID}" != "${SERVER_PID}" ] && kill "${SHELL_PID}" 2>/dev/null || true
    taskkill //F //PID "${SERVER_PID}" 2>/dev/null || true
    kill "${MONITOR_PID}" 2>/dev/null || true
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
        --intensity "${INTENSITY}" \
        --chunk-kib "${CHUNK_KIB}" \
        --vus "${VUS}" \
        --duration "${DURATION}" \
        --target-pid "${SERVER_PID}" \
        --include-children \
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
