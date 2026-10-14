#!/usr/bin/env python3
"""
Executor de experimentos locais integrados (Aplicação + Carga HTTP + Métricas de Sistema).

Executa a aplicação, monitora recursos locais com agendamento monotônico e
gera carga concorrente via threads Python. Útil para testes de validação rápidos
sem necessidade do k6 ou para ambientes autônomos.
"""

import argparse
import csv
import datetime
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from system_monitor import ProcessTreeMonitor


def parse_args():
    p = argparse.ArgumentParser(description="Executor de experimentos de carga locais.")
    p.add_argument("--target-url", default="http://127.0.0.1:8000")
    p.add_argument("--size-mib", type=int, default=8, help="Tamanho do buffer em MiB (padrão conservador: 8).")
    p.add_argument("--size-mb", type=int, default=None, help="Alias retrocompatível para size-mib.")
    p.add_argument("--iterations", type=int, default=1, help="Iterações de varrida (padrão: 1).")
    p.add_argument("--intensity", type=int, default=None, help="Iterações de hash por requisição (sobrepõe iterations).")
    p.add_argument("--chunk-kib", type=int, default=int(os.getenv("DEFAULT_CHUNK_KIB", "64")), help="Bloco hasheado por iteração em KiB.")
    p.add_argument("--vus", type=int, default=3, help="Usuários virtuais simultâneos.")
    p.add_argument("--duration", type=int, default=30, help="Duração do teste em segundos.")
    p.add_argument("--out-dir", default="data/results")
    p.add_argument("--label", default="local")
    p.add_argument("--start-server", action="store_true", help="Iniciar servidor uvicorn automaticamente.")
    p.add_argument("--workers", type=int, default=1, help="Número de workers uvicorn (default: 1 para baseline de 1 vCPU).")
    p.add_argument(
        "--target-pid",
        type=int,
        default=None,
        help=(
            "PID do servidor da aplicação. Habilita proc_rss_mib/proc_cpu_percent "
            "no CSV por nível de demanda — sem isso o generate_plots cai no fallback "
            "de métricas sistêmicas agregadas e o perfil da aplicação fica invisível."
        ),
    )
    p.add_argument(
        "--include-children",
        action="store_true",
        help="Soma RSS/CPU dos descendentes de --target-pid (necessário com --workers > 1).",
    )
    p.add_argument(
        "--load-engine",
        choices=["auto", "k6", "python"],
        default="auto",
        help=(
            "Gerador de carga. 'auto' usa k6 quando disponível. O k6 (Go) é o "
            "padrão porque as threads Python compartilham o GIL com o próprio "
            "cliente: em alto concurrency o gerador vira o gargalo e o RPS cai "
            "conforme os VUs sobem, mascarando a saturação da CPU."
        ),
    )
    p.add_argument(
        "--k6-script",
        default=None,
        help="Caminho do script k6 do projeto (ex.: k6/load-test.js).",
    )
    args = p.parse_args()

    if args.size_mb is not None:
        args.size_mib = args.size_mb
    return args


def validate_target_pid(pid: int, include_children: bool) -> None:
    """
    Aborta se `pid` não for um processo vivo e legível.

    Protege contra o cenário em que o uvicorn falhou ao subir (ex.: porta já em
    uso) e um servidor órfão de execução anterior responde ao /health: nesse caso
    o PID capturado por $! está morto, e sem esta validação as métricas de
    processo sairiam zeradas ou ausentes, gerando gráficos falsos.
    """
    import psutil

    try:
        proc = psutil.Process(pid)
    except psutil.NoSuchProcess:
        raise SystemExit(
            f"[exp][erro] O PID {pid} não existe. O servidor uvicorn morreu — "
            "verifique /tmp/sweep_uvicorn.log (provável porta já em uso)."
        )
    if proc.status() == psutil.STATUS_ZOMBIE:
        raise SystemExit(f"[exp][erro] O PID {pid} é um zumbi: o servidor já encerrou.")
    rss = proc.memory_info().rss / (1024 ** 2)
    if rss <= 0:
        raise SystemExit(
            f"[exp][erro] O PID {pid} reportou RSS=0 MiB — não é o servidor real."
        )
    tree = [proc]
    if include_children:
        try:
            tree += proc.children(recursive=True)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    total = 0.0
    for p in tree:
        try:
            total += p.cpu_percent(interval=0.1)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    print(f"[exp] PID {pid} validado: RSS={rss:.1f} MiB | árvore={len(tree)} processo(s)")


class SystemMetricsCollector:
    """Coletor interno de métricas com agendamento monotônico sem acúmulo de atraso."""
    def __init__(self, interval: float, out_path: str, target_pid: int = None,
                 include_children: bool = False):
        self.interval = max(0.1, interval)
        self.out_path = out_path
        self.target_pid = target_pid
        self.include_children = include_children
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self.samples = 0

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self):
        import psutil

        proc = None
        if self.target_pid:
            try:
                proc = ProcessTreeMonitor(self.target_pid, self.include_children)
            except Exception:
                proc = None

        num_cores = psutil.cpu_count(logical=True) or 1
        psutil.cpu_percent(interval=None)
        psutil.cpu_percent(percpu=True, interval=None)

        fieldnames = ["timestamp_utc", "elapsed_s", "cpu_total_pct"]
        for i in range(num_cores):
            fieldnames.append(f"cpu_core_{i}_pct")
        fieldnames += [
            "mem_used_mib", "mem_percent", "swap_used_mib",
            "disk_read_mib_s", "disk_write_mib_s",
            "disk_read_iops", "disk_write_iops",
        ]
        if proc:
            fieldnames += ["proc_rss_mib", "proc_cpu_percent"]

        try:
            prev_dio = psutil.disk_io_counters()
        except Exception:
            prev_dio = None

        t_start = time.perf_counter()
        prev_sample_perf = t_start
        sample_index = 0

        with open(self.out_path, "w", newline="", buffering=1024) as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            f.flush()

            while not self._stop.is_set():
                sample_index += 1
                target_time = t_start + (sample_index * self.interval)
                sleep_time = target_time - time.perf_counter()
                if sleep_time > 0:
                    time.sleep(sleep_time)

                now_perf = time.perf_counter()
                elapsed = now_perf - t_start
                delta_t = max(1e-3, now_perf - prev_sample_perf)
                prev_sample_perf = now_perf
                vm = psutil.virtual_memory()
                sw = psutil.swap_memory()
                cpu_total = psutil.cpu_percent(interval=None)
                cpu_cores = psutil.cpu_percent(percpu=True, interval=None)

                try:
                    dio = psutil.disk_io_counters()
                except Exception:
                    dio = None
                if dio is not None and prev_dio is not None:
                    disk_read_mib_s = round(
                        max(0, dio.read_bytes - prev_dio.read_bytes) / (1024 ** 2) / delta_t, 3
                    )
                    disk_write_mib_s = round(
                        max(0, dio.write_bytes - prev_dio.write_bytes) / (1024 ** 2) / delta_t, 3
                    )
                    disk_read_iops = round(
                        max(0, dio.read_count - prev_dio.read_count) / delta_t, 2
                    )
                    disk_write_iops = round(
                        max(0, dio.write_count - prev_dio.write_count) / delta_t, 2
                    )
                else:
                    disk_read_mib_s = disk_write_mib_s = 0.0
                    disk_read_iops = disk_write_iops = 0.0
                prev_dio = dio

                row = {
                    "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "elapsed_s": round(elapsed, 3),
                    "cpu_total_pct": cpu_total,
                }
                for i, v in enumerate(cpu_cores):
                    row[f"cpu_core_{i}_pct"] = v
                row.update({
                    "mem_used_mib": round(vm.used / (1024 ** 2), 2),
                    "mem_percent": vm.percent,
                    "swap_used_mib": round(sw.used / (1024 ** 2), 2),
                    "disk_read_mib_s": disk_read_mib_s,
                    "disk_write_mib_s": disk_write_mib_s,
                    "disk_read_iops": disk_read_iops,
                    "disk_write_iops": disk_write_iops,
                })
                if proc:
                    if sample_index % 5 == 0:
                        proc.refresh()
                    try:
                        row["proc_rss_mib"] = round(proc.rss_mib(), 2)
                        row["proc_cpu_percent"] = round(proc.cpu_percent(), 2)
                    except Exception:
                        row["proc_rss_mib"] = ""
                        row["proc_cpu_percent"] = ""
                        proc = None

                writer.writerow(row)
                f.flush()
                self.samples += 1


def make_request(url: str) -> dict:
    t0 = time.perf_counter()
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            duration_ms = (time.perf_counter() - t0) * 1000
            try:
                body = json.loads(raw)
                timing = body.get("timing", {})
                app_total = timing.get("app_total_ms", timing.get("total_ms"))
            except Exception:
                app_total = None

            return {
                "ok": resp.status == 200,
                "status": resp.status,
                "duration_ms": duration_ms,
                "app_total_ms": app_total,
            }
    except urllib.error.HTTPError as e:
        duration_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": False,
            "status": e.code,
            "duration_ms": duration_ms,
            "error": f"HTTPError: {e.code}",
        }
    except Exception as e:
        duration_ms = (time.perf_counter() - t0) * 1000
        return {
            "ok": False,
            "status": 0,
            "duration_ms": duration_ms,
            "error": str(e),
        }


def run_load(url: str, vus: int, duration: int) -> list:
    results = []
    lock = threading.Lock()
    stop_event = threading.Event()

    def worker():
        while not stop_event.is_set():
            r = make_request(url)
            with lock:
                results.append(r)
            time.sleep(0.05)  # ritmo controlado

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(vus)]
    for t in threads:
        t.start()

    time.sleep(duration)
    stop_event.set()

    for t in threads:
        t.join(timeout=35)

    return results


def k6_available() -> bool:
    """True se o binário `k6` estiver no PATH."""
    return shutil.which("k6") is not None


def run_load_k6(k6_script: str, vus: int, duration: int, env: dict) -> dict:
    """
    Executa um nível de demanda via k6 e devolve o resumo agregado no mesmo
    formato de `summarize()`.

    Motivo: o gerador de carga em threads Python (`run_load`) é limitado pelo
    GIL. Em alto concurrency o próprio cliente satura ~1 núcleo e compete pela
    CPU com o servidor na mesma máquina, fazendo o RPS CAIR conforme os VUs
    sobem (medido: 3,64 RPS @1 VU → 2,02 RPS @32 VUs) — o oposto do que se
    espera de um workload CPU-bound. O k6 é escrito em Go, não compartilha o
    GIL e consegue sustentar a pressão real sobre a aplicação.
    """
    proj_root = os.path.dirname(os.path.dirname(os.path.abspath(k6_script)))
    summary_path = tempfile.mktemp(suffix=".json", prefix="k6_summary_")
    # O handleSummary do k6 grava um k6_*.csv em OUT_DIR. Como o orquestrador já
    # grava o exp_*.csv canônico, redireciona-se para um diretório temporário a
    # fim de não duplicar cada nível de demanda nos gráficos.
    scratch_dir = tempfile.mkdtemp(prefix="k6_scratch_")

    k6_env = os.environ.copy()
    k6_env.update({k: str(v) for k, v in env.items()})
    k6_env["OUT_DIR"] = scratch_dir
    cmd = [
        "k6", "run", "--quiet", "--summary-export", summary_path,
        "-e", f"BASE_URL={env.get('BASE_URL', env.get('TARGET_URL', ''))}",
        "-e", f"VUS={vus}",
        "-e", f"DURATION={duration}s",
        "-e", f"OUT_DIR={scratch_dir}",
    ]
    for k, v in env.items():
        if k in ("BASE_URL", "TARGET_URL"):
            continue
        cmd += ["-e", f"{k}={v}"]
    cmd.append(k6_script)

    proc = subprocess.run(cmd, cwd=proj_root, env=k6_env,
                          capture_output=True, text=True)
    if proc.returncode not in (0, 99):  # 99 = thresholds reprovados, dados válidos
        raise RuntimeError(
            f"k6 falhou (exit={proc.returncode}).\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )
    if not os.path.exists(summary_path):
        raise RuntimeError(f"k6 não gerou o resumo em {summary_path}.\n{proc.stderr}")

    with open(summary_path) as f:
        raw = json.load(f)
    os.unlink(summary_path)
    shutil.rmtree(scratch_dir, ignore_errors=True)

    m = raw.get("metrics", {})

    def val(name, key, default=None):
        # O --summary-export do k6 devolve as métricas com os valores planos
        # ({"count": N, "rate": X, "p(95)": Y}), e não aninhados sob "values"
        # como no objeto de runtime. Ler só "values" fazia todos os campos
        # voltarem 0 e o CSV de experimento sair zerado.
        node = m.get(name)
        if not isinstance(node, dict):
            return default
        values = node.get("values")
        if isinstance(values, dict) and key in values:
            return values[key]
        if key in node:
            return node[key]
        return default

    reqs = int(val("http_reqs", "count", 0) or 0)
    rps = float(val("http_reqs", "rate", 0.0) or 0.0)
    failed = float(val("http_req_failed", "rate", 0.0) or 0.0)
    avg = float(val("http_req_duration", "avg", 0.0) or 0.0)
    p50 = float(val("http_req_duration", "med", 0.0) or 0.0)
    p90 = float(val("http_req_duration", "p(90)", 0.0) or 0.0)
    p95 = float(val("http_req_duration", "p(95)", 0.0) or 0.0)
    dmax = float(val("http_req_duration", "max", 0.0) or 0.0)

    return {
        "total_requests": reqs,
        "errors": int(round(failed * reqs)),
        "error_rate": round(failed, 4),
        "latency_avg_ms": round(avg, 2),
        "latency_med_ms": round(p50, 2),
        "latency_p90_ms": round(p90, 2),
        "latency_p95_ms": round(p95, 2),
        "latency_max_ms": round(dmax, 2),
        "measured_rps": round(rps, 2),
        "load_engine": "k6",
    }


def summarize(results: list) -> dict:
    durations = sorted(r["duration_ms"] for r in results)
    n = len(durations)
    errors = sum(1 for r in results if not r["ok"])

    def pct(p):
        if not durations:
            return 0.0
        idx = int(p / 100 * n)
        return round(durations[min(idx, n - 1)], 2)

    return {
        "total_requests": n,
        "errors": errors,
        "error_rate": round(errors / n, 4) if n > 0 else 0.0,
        "latency_avg_ms": round(sum(durations) / n, 2) if n > 0 else 0.0,
        "latency_med_ms": pct(50),
        "latency_p90_ms": pct(90),
        "latency_p95_ms": pct(95),
        "latency_max_ms": round(durations[-1], 2) if durations else 0.0,
    }


def save_results(summary: dict, args, ts_str: str, out_dir: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    fname = os.path.join(out_dir, f"exp_{args.label}_{ts_str}.csv")

    rows = [("metric", "value")]
    rows.append(("test_run_id", f"exp_{args.label}_{ts_str}"))
    rows.append(("timestamp_utc", datetime.datetime.now(datetime.timezone.utc).isoformat()))
    rows.append(("target_url", args.target_url))
    rows.append(("size_mib", args.size_mib))
    rows.append(("size_mb", args.size_mib))  # alias
    rows.append(("iterations", args.iterations))
    if getattr(args, "resolved_intensity", None) is not None:
        rows.append(("intensity", args.resolved_intensity))
        rows.append(("chunk_kib", args.chunk_kib))
    if getattr(args, "dwell_ms", None):
        rows.append(("dwell_ms", args.dwell_ms))
    rows.append(("vus", args.vus))
    rows.append(("duration_s", args.duration))
    for k, v in summary.items():
        rows.append((k, v))

    with open(fname, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(f"[exp] Resultados salvos com sucesso → {fname}")
    return fname


def wait_server(url: str, retries: int = 30) -> bool:
    for _ in range(retries):
        try:
            req = urllib.request.Request(f"{url}/health")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


def main():
    args = parse_args()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ts_str = now_utc.strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(args.out_dir, exist_ok=True)

    target_clean = args.target_url.rstrip("/")
    if args.intensity is not None:
        intensity = min(max(1, args.intensity), 50000)
    else:
        intensity = min(max(1, args.iterations * 1000), 50000)
    args.resolved_intensity = intensity
    workload_url = (
        f"{target_clean}/process-image"
        f"?intensity={intensity}&chunk_kib={args.chunk_kib}"
    )

    server_proc = None
    if args.start_server:
        proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        uvicorn_bin = os.path.join(proj_root, ".venv", "bin", "uvicorn")
        log_f = open("/tmp/exp_server.log", "w")
        server_proc = subprocess.Popen(
            [uvicorn_bin, "app.main:app", "--host", "127.0.0.1",
             "--port", "8000", "--workers", str(args.workers)],
            cwd=proj_root,
            stdout=log_f,
            stderr=log_f,
        )
        print("[exp] Aguardando servidor iniciar…", end=" ", flush=True)
        if not wait_server(target_clean):
            print("FALHOU")
            log_f.close()
            with open("/tmp/exp_server.log", "r") as lf:
                print(lf.read())
            server_proc.terminate()
            sys.exit(1)
        print("OK")

    sys_csv = os.path.join(args.out_dir, f"sys_local_{args.label}_{ts_str}.csv")

    # Com --start-server o PID é interno; caso contrário o chamador (ex.:
    # run_local_sweep.sh) deve informar --target-pid. Sem isso o CSV por nível
    # fica sem proc_rss_mib/proc_cpu_percent e o gráfico Demanda×Recursos cai
    # no fallback de métricas sistêmicas agregadas.
    if server_proc is not None:
        target_pid = server_proc.pid
        include_children = True  # uvicorn --workers N cria filhos worker
    else:
        target_pid = args.target_pid
        include_children = args.include_children

    if target_pid is not None:
        validate_target_pid(target_pid, include_children)

    monitor = SystemMetricsCollector(
        interval=1.0,
        out_path=sys_csv,
        target_pid=target_pid,
        include_children=include_children,
    )
    if target_pid is None:
        print(
            "[exp][aviso] --target-pid não informado e servidor não iniciado aqui.\n"
            "             O CSV de métricas NÃO terá proc_rss_mib/proc_cpu_percent,\n"
            "             e os gráficos de recursos vs. demanda ficarão planos."
        )
    monitor.start()

    proj_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    k6_script = args.k6_script or os.path.join(proj_root, "k6", "load-test.js")
    engine = args.load_engine
    if engine == "auto":
        engine = "k6" if (k6_available() and os.path.exists(k6_script)) else "python"
    if engine == "k6" and not (k6_available() and os.path.exists(k6_script)):
        raise SystemExit(
            f"[exp][erro] --load-engine=k6, mas k6 ou o script não estão disponíveis.\n"
            f"           k6 no PATH: {k6_available()} | script: {k6_script}"
        )

    print(
        f"[exp] Iniciando carga: {args.vus} VUs × {args.duration}s | "
        f"engine={engine} | intensity={intensity} chunk={args.chunk_kib} KiB"
    )
    t0 = time.perf_counter()
    if engine == "k6":
        summary = run_load_k6(k6_script, args.vus, args.duration, {
            "BASE_URL": target_clean,
            "INTENSITY": intensity,
            "CHUNK_KIB": args.chunk_kib,
        })
    else:
        results = run_load(workload_url, args.vus, args.duration)
        summary = summarize(results)
        summary["measured_rps"] = (
            round(summary["total_requests"] / (time.perf_counter() - t0), 2)
        )
    elapsed = time.perf_counter() - t0

    monitor.stop()

    summary["test_duration_s"] = round(elapsed, 2)
    rps = summary["measured_rps"]

    print(f"\n[exp] Resultados:")
    print(f"      total_requests  = {summary['total_requests']}")
    print(f"      errors          = {summary['errors']} ({summary['error_rate']*100:.1f}%)")
    print(f"      latency_avg_ms  = {summary['latency_avg_ms']}")
    print(f"      latency_p95_ms  = {summary['latency_p95_ms']}")
    print(f"      measured_rps    = {rps}")
    print(f"      sys_metrics     → {sys_csv}")

    save_results(summary, args, ts_str, args.out_dir)

    if server_proc:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=5)
        except Exception:
            server_proc.kill()

        for _ in range(20):
            time.sleep(0.2)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                if s.connect_ex(("127.0.0.1", 8000)) != 0:
                    break


if __name__ == "__main__":
    main()
