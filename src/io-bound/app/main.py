"""
Aplicação IO-bound — FastAPI

Perfil computacional
--------------------
O endpoint `/process` realiza I/O de disco *real*, e não apenas cópia em
page cache:

1. Escreve `size_mb` MiB no disco em blocos (`os.write`), em um arquivo sob
   `IO_DIR` (configurável por variável de ambiente).
2. Chama `os.fsync(fd)` para forçar a descarga física dos dados do page cache
   para o dispositivo de bloco — etapa essencial para que a carga seja
   limitada por disco e não por memória.
3. Lê o arquivo de volta com `os.read`.
4. Remove o arquivo.

Atenção — Diretório de I/O:
---------------------------
Se `IO_DIR` apontar para um sistema de arquivos em RAM (ex.: `/dev/shm` ou um
`/tmp` montado como `tmpfs`), a carga deixa de ser IO-bound. Para os
experimentos, aponte `IO_DIR` para um volume de disco real (ex.: `/data/io` na
VM, montado no disco do VirtualBox).

Unidades: todas as quantidades são em unidades binárias (1 MiB = 1048576 bytes).
"""

import asyncio
import os
import time
import uuid

from fastapi import FastAPI, Query

app = FastAPI(title="IO-bound Workload API", version="2.0.0")

# Diretório de trabalho de I/O — deve ser um volume de disco real.
IO_DIR = os.getenv("IO_DIR", "/tmp")
DEFAULT_SIZE_MB = int(os.getenv("DEFAULT_SIZE_MB", "8"))
DEFAULT_ITERATIONS = int(os.getenv("DEFAULT_ITERATIONS", "1"))
DEFAULT_CHUNK_KB = int(os.getenv("IO_CHUNK_KB", "1024"))
DEFAULT_FSYNC = os.getenv("IO_FSYNC", "1") not in ("0", "false", "False", "")


def _run_io_workload(size_mb: int, iterations: int, chunk_kb: int, do_fsync: bool) -> dict:
    """
    Executa o ciclo write+fsync+read `iterations` vezes sobre um mesmo arquivo.

    Retorna dicionário com tempos discriminados (ms) e bytes transferidos.
    """
    os.makedirs(IO_DIR, exist_ok=True)
    path = os.path.join(IO_DIR, f"io_test_{uuid.uuid4().hex}.dat")

    num_bytes = size_mb * 1024 * 1024
    chunk_bytes = max(1024, chunk_kb * 1024)
    # Conteúdo pseudo-aleatório: evita arquivos esparsos (zeros) e compressão.
    chunk = os.urandom(chunk_bytes)

    t0 = time.perf_counter()
    write_ms = fsync_ms = read_ms = 0.0
    bytes_written = bytes_read = 0

    for _ in range(iterations):
        # --- Escrita ---
        tw0 = time.perf_counter()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            written = 0
            while written < num_bytes:
                n = min(chunk_bytes, num_bytes - written)
                os.write(fd, chunk[:n])
                written += n
            tw1 = time.perf_counter()
            # --- Descarga física para o dispositivo ---
            if do_fsync:
                os.fsync(fd)
            tw2 = time.perf_counter()
        finally:
            os.close(fd)

        write_ms += (tw1 - tw0) * 1000
        fsync_ms += (tw2 - tw1) * 1000
        bytes_written += written

        # --- Leitura ---
        tr0 = time.perf_counter()
        fd = os.open(path, os.O_RDONLY)
        try:
            read_total = 0
            while True:
                b = os.read(fd, chunk_bytes)
                if not b:
                    break
                read_total += len(b)
        finally:
            os.close(fd)
        tr1 = time.perf_counter()
        read_ms += (tr1 - tr0) * 1000
        bytes_read += read_total

    # --- Remoção ---
    try:
        os.remove(path)
    except OSError:
        pass

    total_ms = (time.perf_counter() - t0) * 1000

    return {
        "status": "ok",
        "params": {
            "size_mb": size_mb,
            "iterations": iterations,
            "chunk_kb": chunk_kb,
            "fsync": do_fsync,
            "io_dir": IO_DIR,
        },
        "timing": {
            "write_ms": round(write_ms, 3),
            "fsync_ms": round(fsync_ms, 3),
            "read_ms": round(read_ms, 3),
            "app_total_ms": round(total_ms, 3),
        },
        "io": {
            "bytes_written": bytes_written,
            "bytes_read": bytes_read,
            "mib_written": round(bytes_written / (1024 ** 2), 3),
            "mib_read": round(bytes_read / (1024 ** 2), 3),
        },
    }


@app.get("/health")
def health():
    return {"status": "ok", "io_dir": IO_DIR}


@app.get("/info")
def info():
    return {
        "app": "io-bound-api",
        "version": "2.0.0",
        "workload_profile": "disk-io (write+fsync+read em volume de disco)",
        "io_dir": IO_DIR,
        "defaults": {
            "size_mb": DEFAULT_SIZE_MB,
            "iterations": DEFAULT_ITERATIONS,
            "chunk_kb": DEFAULT_CHUNK_KB,
            "fsync": DEFAULT_FSYNC,
        },
    }


@app.get("/process", summary="Carga IO-bound: escrita com fsync + leitura em disco")
async def process(
    size_mb: int = Query(default=DEFAULT_SIZE_MB, ge=1, le=1024,
                         description="Tamanho do arquivo em MiB (1–1024)."),
    iterations: int = Query(default=DEFAULT_ITERATIONS, ge=1, le=50,
                            description="Número de ciclos write+fsync+read."),
    chunk_kb: int = Query(default=DEFAULT_CHUNK_KB, ge=1, le=65536,
                          description="Tamanho do bloco de escrita/leitura em KiB."),
    fsync: bool = Query(default=DEFAULT_FSYNC,
                        description="Se true, força descarga física (fsync) — essencial para IO-bound."),
):
    # Executa o I/O fora do event loop (threadpool) para não bloquear o servidor.
    return await asyncio.to_thread(
        _run_io_workload, size_mb, iterations, chunk_kb, fsync
    )
