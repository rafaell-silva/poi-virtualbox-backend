"""
Aplicação Memory-bound — FastAPI

Perfil computacional e considerações arquiteturais
--------------------------------------------------
A carga de trabalho desta aplicação é focada em *pressão sobre a capacidade
de memória* (memory capacity pressure), diferenciando-se de *saturação da
largura de banda do barramento DRAM* (memory bandwidth saturation):

- **Pressão sobre capacidade (Capacity Pressure)**:
  Aloca blocos contíguos de memória (`bytearray`) na heap do processo e toca
  ativamente suas páginas (page touching) para forçar o kernel Linux a mapear
  páginas físicas (evitando lazy page allocation e zero-page deduplication do kernel).
  A retenção desse buffer durante o ciclo de vida da requisição eleva o RSS
  (Resident Set Size) do processo. Sob escassez de RAM física, o sistema
  operacional é forçado a acionar swap/paginação, provocando expressivo aumento
  de latência.

- **Saturação de largura de banda (Bandwidth Saturation)**:
  Exige transferências contínuas e concorrentes no limite teórico do barramento
  CPU–DRAM (tipicamente 15–50+ GB/s). Em ambientes de alto nível como Python/CPython,
  loops iterativos byte-a-byte introduzem sobrecarga de interpretação de bytecode,
  tornando a execução limitada pela CPU e pelo interpretador, em vez da memória.
  Nesta implementação, a manipulação de dados é realizada via operações nativas de
  fatiamento em blocos (`slice assignment` em C via CPython), reduzindo drasticamente
  o overhead do interpretador e garantindo que o tempo gasto seja primariamente
  devido à alocação, materialização e retenção da memória física. Não se assume
  nem se promete garantia de saturação do barramento DRAM, dadas as características
  da linguagem e do ambiente de virtualização.

Unidades de medida
------------------
Todas as quantidades de memória são tratadas em unidades binárias:
- 1 KiB = 1024 bytes
- 1 MiB = 1024 * 1024 bytes (1 048 576 bytes)
- 1 GiB = 1024 * 1024 * 1024 bytes

Segurança e limites de concorrência
-----------------------------------
Para evitar esgotamento descontrolado de RAM e acionamento do OOM Killer do Linux
(especialmente em VMs de referência de 1 vCPU / 1 GiB RAM):
- `MAX_SIZE_MIB`: limite máximo para uma requisição individual (padrão: 256 MiB).
- `MAX_CONCURRENT_MIB`: teto agregado de memória alocada por requisições simultâneas
  dentro do processo worker (padrão: 384 MiB). Caso uma requisição exceda este teto,
  a API rejeita imediatamente com HTTP 503 (Service Unavailable).
  *Nota*: Este limite é por processo worker. Se forem executados N workers, o teto
  agregado do sistema será multiplicado pelo número de workers ativos.

Defaults do workload
--------------------
- `DEFAULT_SIZE_MIB` (8 MiB) e `DEFAULT_DWELL_MS` (200 ms). O dwell é o
  parâmetro que materializa a pressão de capacidade: `time.sleep` libera o GIL,
  permitindo que outras requisições aloquem os seus buffers enquanto o buffer
  anterior ainda está retido. Sem dwell, o buffer morre em milissegundos e o RSS
  não acompanha o aumento da demanda. O tamanho é mantido em 8 MiB para deixar
  folga sob `MAX_CONCURRENT_MIB` (≈48 buffers simultâneos), evitando HTTP 503
  nos níveis de demanda da varredura.
"""

import datetime
import os
import threading
import time

import psutil
from fastapi import FastAPI, HTTPException, Query, Response
from fastapi.responses import JSONResponse

# --------------------------------------------------------------------------- #
# Configurações de Ambiente com Fallbacks Seguros
# --------------------------------------------------------------------------- #
def _get_env_int(key: str, default: int, min_val: int = 1) -> int:
    try:
        val = int(os.getenv(key, str(default)))
        return max(min_val, val)
    except ValueError:
        return default


# Padrões adequados a ambientes de 1 GiB RAM.
# DEFAULT_DWELL_MS é o parâmetro que faz o workload de fato aparecer como
# memory-bound nos gráficos:
#   * Com dwell=0 o buffer morre em ~6 ms e a janela de residência da memória é
#     minúscula frente ao período de amostragem (1 s) — o RSS mal se move.
#   * O dwell retém o buffer por tempo suficiente para que outras requisições
#     (o sleep libera o GIL) aloquem enquanto a anterior ainda não liberou a sua,
#     fazendo o RSS crescer de forma monotônica e proporcional à demanda
#     (medido localmente: 55 MiB com 1 VU → 554 MiB com 32 VUs, 0 erros).
# DEFAULT_SIZE_MIB=8 mantém folga sob a cota MAX_CONCURRENT_MIB=384 MiB
# (≈48 buffers simultâneos), de modo que os 32 VUs da varredura local não
# disparam HTTP 503 e a curva de RSS permanece limpa.
MAX_SIZE_MIB: int = _get_env_int("MAX_SIZE_MIB", 256)
DEFAULT_SIZE_MIB: int = _get_env_int("DEFAULT_SIZE_MIB", 8)
DEFAULT_ITERATIONS: int = _get_env_int("DEFAULT_ITERATIONS", 1)
DEFAULT_DWELL_MS: int = _get_env_int("DEFAULT_DWELL_MS", 200, min_val=0)
MAX_CONCURRENT_MIB: int = _get_env_int("MAX_CONCURRENT_MIB", 384)

# Bloco base para operações de fatiamento (64 KiB — minimiza overhead CPython)
CHUNK_SIZE_BYTES: int = 64 * 1024
CHUNK_PATTERN: bytes = bytes([(i % 251) for i in range(CHUNK_SIZE_BYTES)])

app = FastAPI(
    title="Memory-bound Workload API",
    description="Aplicação para estudo de demanda, provisionamento e consumo de memória.",
    version="1.1.0",
)

START_TIME = datetime.datetime.now(datetime.timezone.utc)

# --------------------------------------------------------------------------- #
# Controle de Concorrência de Memória (Thread-safe por processo)
# --------------------------------------------------------------------------- #
_mem_lock = threading.Lock()
_current_allocated_bytes: int = 0


def _try_reserve_memory(bytes_needed: int) -> bool:
    """Tenta reservar cota de memória para a requisição. Retorna True se aprovado."""
    global _current_allocated_bytes
    max_bytes = MAX_CONCURRENT_MIB * 1024 * 1024
    with _mem_lock:
        if _current_allocated_bytes + bytes_needed <= max_bytes:
            _current_allocated_bytes += bytes_needed
            return True
        return False


def _release_memory(bytes_needed: int) -> None:
    """Libera a cota de memória reservada."""
    global _current_allocated_bytes
    with _mem_lock:
        _current_allocated_bytes = max(0, _current_allocated_bytes - bytes_needed)


def _get_current_allocated_mib() -> float:
    with _mem_lock:
        return round(_current_allocated_bytes / (1024 ** 2), 2)


# --------------------------------------------------------------------------- #
# Utilitários de Diagnóstico de Memória
# --------------------------------------------------------------------------- #
def _current_process_mem_mib() -> float:
    """Retorna o Resident Set Size (RSS) do processo atual em MiB."""
    proc = psutil.Process(os.getpid())
    return proc.memory_info().rss / (1024 ** 2)


def _system_mem() -> dict:
    """Snapshot das métricas do sistema operacional."""
    vm = psutil.virtual_memory()
    sw = psutil.swap_memory()
    return {
        "total_mib": round(vm.total / (1024 ** 2), 2),
        "available_mib": round(vm.available / (1024 ** 2), 2),
        "used_mib": round(vm.used / (1024 ** 2), 2),
        "percent": vm.percent,
        "swap_total_mib": round(sw.total / (1024 ** 2), 2),
        "swap_used_mib": round(sw.used / (1024 ** 2), 2),
        "swap_percent": sw.percent,
    }


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health", summary="Verificação de disponibilidade e status de recursos")
def health():
    """Retorna disponibilidade da aplicação, limites configurados e uso atual de recursos."""
    now = datetime.datetime.now(datetime.timezone.utc)
    uptime = (now - START_TIME).total_seconds()
    return {
        "status": "ok",
        "uptime_seconds": round(uptime, 2),
        "process_pid": os.getpid(),
        "process_rss_mib": round(_current_process_mem_mib(), 2),
        "reserved_concurrency_mib": _get_current_allocated_mib(),
        "max_concurrent_limit_mib": MAX_CONCURRENT_MIB,
        "system_memory": _system_mem(),
        "timestamp_utc": now.isoformat(),
    }


@app.get("/info", summary="Informações arquiteturais e limites de segurança")
def info():
    return {
        "app": "memory-bound-api",
        "version": "1.1.0",
        "unit": "MiB (1024 * 1024 bytes)",
        "workload_profile": "capacity-pressure (in-memory buffer retention & block touching)",
        "limits": {
            "max_size_mib_per_request": MAX_SIZE_MIB,
            "max_concurrent_mib_per_worker": MAX_CONCURRENT_MIB,
            "default_size_mib": DEFAULT_SIZE_MIB,
            "default_iterations": DEFAULT_ITERATIONS,
            "default_dwell_ms": DEFAULT_DWELL_MS,
        },
        "system_memory": _system_mem(),
    }


@app.get("/workload", summary="Carga de trabalho memory-bound com retenção e acesso a buffers")
def workload(
    response: Response,
    size_mib: int = Query(
        default=DEFAULT_SIZE_MIB,
        ge=1,
        le=MAX_SIZE_MIB,
        description=f"Tamanho do buffer alocado em MiB (1–{MAX_SIZE_MIB}).",
    ),
    size_mb: int = Query(
        default=None,
        ge=1,
        le=MAX_SIZE_MIB,
        description="Alias retrocompatível para size_mib.",
    ),
    iterations: int = Query(
        default=DEFAULT_ITERATIONS,
        ge=1,
        le=100,
        description="Número de varridas de leitura/escrita em blocos sobre o buffer.",
    ),
    dwell_ms: int = Query(
        default=DEFAULT_DWELL_MS,
        ge=0,
        le=10000,
        description="Tempo (ms) de retenção do buffer alocado antes de liberá-lo.",
    ),
) -> JSONResponse:
    """
    Executa carga de trabalho memory-bound:
    1. Reserva cota agregada de memória no worker (HTTP 503 se exceder capacidade).
    2. Registra snapshot de memória 'before'.
    3. Aloca `size_mib` MiB em heap como `bytearray`.
    4. Realiza 'page touching' escrevendo blocos pré-construídos para materializar o RSS.
    5. Executa `iterations` varridas completas em bloco.
    6. Registra snapshot 'during' com o buffer ativo.
    7. Desaloca explicitamente o buffer e registra snapshot 'after_release'.
    8. Retorna métricas temporais discriminadas (alloc, work, app_total).
    """
    # Garantir que respostas de teste não sejam armazenadas em caches HTTP intermediários
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"

    effective_size_mib = size_mb if size_mb is not None else size_mib
    num_bytes = effective_size_mib * 1024 * 1024

    # Proteção de concorrência agregada por processo worker
    if not _try_reserve_memory(num_bytes):
        return JSONResponse(
            status_code=503,
            headers={
                "Retry-After": "1",
                "Cache-Control": "no-store",
            },
            content={
                "status": "error",
                "code": "CONCURRENT_MEMORY_LIMIT_EXCEEDED",
                "message": (
                    f"Capacidade de memória concorrente do worker esgotada. "
                    f"Alocado atualmente: {_get_current_allocated_mib()} MiB. "
                    f"Solicitado: {effective_size_mib} MiB. "
                    f"Limite por worker: {MAX_CONCURRENT_MIB} MiB."
                ),
                "limits": {
                    "max_concurrent_mib": MAX_CONCURRENT_MIB,
                    "current_allocated_mib": _get_current_allocated_mib(),
                    "requested_mib": effective_size_mib,
                },
                "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )

    try:
        # --- Fase 1: Snapshot Before ---
        t0 = time.perf_counter()
        mem_before_proc = _current_process_mem_mib()
        mem_before_sys = _system_mem()

        # --- Fase 2: Alocação ---
        # Aloca memória no heap como bytearray
        buf = bytearray(num_bytes)
        t_alloc = time.perf_counter()
        alloc_ms = round((t_alloc - t0) * 1000, 3)

        # --- Fase 3: Materialização e Processamento (Work) ---
        # Manipulação em blocos usando fatiamento nativo em C (sem loops byte-a-byte no CPython)
        chunk_len = CHUNK_SIZE_BYTES
        pattern = CHUNK_PATTERN
        checksum = 0

        for it in range(iterations):
            # Escrita em blocos: toca cada página física da região de memória
            offset = 0
            while offset < num_bytes:
                end = min(offset + chunk_len, num_bytes)
                buf[offset:end] = pattern[: end - offset]
                offset = end

            # Leitura amostral em múltiplos pontos do buffer para evitar eliminação de código
            checksum ^= buf[0] ^ buf[num_bytes // 2] ^ buf[num_bytes - 1] ^ (it & 0xFF)

        t_work = time.perf_counter()
        work_ms = round((t_work - t_alloc) * 1000, 3)

        # --- Fase 3b: Retenção do buffer (dwell) ---
        # Mantém a memória alocada por um tempo, aumentando a janela de pressão
        # sobre a capacidade de RAM (e a chance de acionar swap sob concorrência).
        if dwell_ms > 0:
            time.sleep(dwell_ms / 1000.0)

        # --- Fase 4: Snapshot During (buffer ainda retido em memória) ---
        mem_during_proc = _current_process_mem_mib()
        mem_during_sys = _system_mem()

        # --- Fase 5: Liberação explícita ---
        del buf
        t_release = time.perf_counter()

        # --- Fase 6: Snapshot After Release ---
        mem_after_proc = _current_process_mem_mib()
        mem_after_sys = _system_mem()

        app_total_ms = round((t_release - t0) * 1000, 3)

        return JSONResponse(
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate",
            },
            content={
                "status": "ok",
                "params": {
                    "size_mib": effective_size_mib,
                    "iterations": iterations,
                    "dwell_ms": dwell_ms,
                    "chunk_size_kib": round(chunk_len / 1024, 1),
                },
                "timing": {
                    "alloc_ms": alloc_ms,
                    "work_ms": work_ms,
                    "dwell_ms": dwell_ms,
                    "app_total_ms": app_total_ms,
                    "description": (
                        "alloc_ms: tempo de alocação inicial do bytearray; "
                        "work_ms: escrita/leitura em blocos tocando as páginas; "
                        "dwell_ms: tempo de retenção do buffer; "
                        "app_total_ms: tempo total de execução interna da requisição."
                    ),
                },
                "memory_snapshots": {
                    "unit": "MiB",
                    "note": (
                        "Snapshots pontuais representam o estado nos instantes de execução. "
                        "Para aferição contínua do pico e efeito no SO, utilize o coletor externo."
                    ),
                    "before": {
                        "process_rss_mib": round(mem_before_proc, 2),
                        "system": mem_before_sys,
                    },
                    "during": {
                        "process_rss_mib": round(mem_during_proc, 2),
                        "delta_process_rss_mib": round(mem_during_proc - mem_before_proc, 2),
                        "system": mem_during_sys,
                    },
                    "after_release": {
                        "process_rss_mib": round(mem_after_proc, 2),
                        "system": mem_after_sys,
                    },
                },
                "checksum": checksum,
                "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            },
        )
    finally:
        _release_memory(num_bytes)
