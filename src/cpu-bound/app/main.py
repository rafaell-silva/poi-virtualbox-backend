"""
PoC - Serviço de processamento de imagem simulado.

Endpoints:
  GET  /health          -> liveness/readiness (leve)
  GET  /info            -> mostra qual Pod respondeu (bom para ver o balanceamento)
  POST /process-image   -> simula processamento pesado (consome CPU de verdade)
  GET  /process-image   -> mesma coisa via GET (facilita teste rápido no navegador/curl)
"""
import hashlib
import os
import socket
import time
import uuid

from fastapi import FastAPI, Query

app = FastAPI(title="Image Processor PoC", version="1.0.0")

POD_NAME = os.getenv("HOSTNAME", socket.gethostname())
APP_VERSION = os.getenv("APP_VERSION", "v1")

# Parâmetros do workload (ajustáveis por ambiente).
DEFAULT_INTENSITY = int(os.getenv("DEFAULT_INTENSITY", "1000"))
DEFAULT_CHUNK_KIB = int(os.getenv("DEFAULT_CHUNK_KIB", "64"))

# O CPython só libera o GIL no hashlib para entradas >= ~2 KiB. Ao hashear um
# bloco grande por iteração, threads concorrentes calculam em paralelo e o
# threadpool do FastAPI consegue usar VÁRIOS núcleos com um único worker —
# o consumo de CPU cresce de verdade com a concorrência.
_MIN_GIL_RELEASE_BYTES = 2048

_CHUNK_CACHE: dict[int, bytes] = {}


def _chunk(kib: int) -> bytes:
    """Buffer determinístico de `kib` KiB (cacheado por tamanho)."""
    if kib not in _CHUNK_CACHE:
        _CHUNK_CACHE[kib] = bytes(range(256)) * (kib * 4)  # 256 B * 4k = k KiB
    return _CHUNK_CACHE[kib]


def simulate_image_processing(intensity: int, chunk_kib: int) -> dict:
    """
    Simula o tratamento de uma imagem aplicando hashes SHA-256 repetidos sobre
    um bloco de `chunk_kib` KiB (resize/filtro/compressão fictícios). É CPU-bound
    de propósito: o trabalho por requisição cresce linearmente com `intensity`
    e com `chunk_kib`, permitindo saturar a CPU.
    """
    start = time.perf_counter()

    buf = _chunk(chunk_kib)
    digest = hashlib.sha256(buf).digest()
    for _ in range(intensity):
        h = hashlib.sha256()
        h.update(digest)
        h.update(buf)  # >= 2 KiB: libera o GIL e permite paralelismo real
        digest = h.digest()

    elapsed_ms = (time.perf_counter() - start) * 1000
    return {
        "image_id": str(uuid.uuid4()),
        "status": "processed",
        "filters_applied": ["resize", "sharpen", "compress"],
        "intensity": intensity,
        "chunk_kib": chunk_kib,
        "checksum": digest.hex()[:16],
        "processing_time_ms": round(elapsed_ms, 2),
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/info")
def info():
    return {"pod": POD_NAME, "version": APP_VERSION}


@app.api_route("/process-image", methods=["GET", "POST"])
def process_image(
    intensity: int = Query(
        default=DEFAULT_INTENSITY,
        ge=1,
        le=50000,
        description="Quantidade de iterações de hash (controla o uso de CPU)",
    ),
    chunk_kib: int = Query(
        default=DEFAULT_CHUNK_KIB,
        ge=1,
        le=1024,
        description="Tamanho do bloco hasheado por iteração em KiB (>=2 libera o GIL)",
    ),
):
    result = simulate_image_processing(intensity, chunk_kib)
    result["served_by"] = POD_NAME
    return result
