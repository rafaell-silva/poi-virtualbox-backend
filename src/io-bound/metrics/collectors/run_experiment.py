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
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request


def parse_args():
    p = argparse.ArgumentParser(description="Executor de experimentos de carga locais.")
    p.add_argument("--target-url", default="http://127.0.0.1:8000")
    p.add_argument("--size-mib", type=int, default=8, help="Tamanho do buffer em MiB (padrão conservador: 8).")
    p.add_argument("--size-mb", type=int, default=None, help="Alias retrocompatível para size-mib.")
    p.add_argument("--iterations", type=int, default=1, help="Iterações de varrida (padrão: 1).")
    p.add_argument("--vus", type=int, default=3, help="Usuários virtuais simultâneos.")
    p.add_argument("--duration", type=int, default=30, help="Duração do teste em segundos.")
    p.add_argument("--out-dir", default="data/results")
    p.add_argument("--label", default="local")
    p.add_argument("--start-server", action="store_true", help="Iniciar servidor uvicorn automaticamente.")
    p.add_argument("--workers", type=int, default=1, help="Número de workers uvicorn (default: 1 para baseline de 1 vCPU).")
    args = p.parse_args()

    if args.size_mb is not None:
        args.size_mib = args.size_mb
    return args


class SystemMetricsCollector:
    """Coletor interno de métricas com agendamento monotônico sem acúmulo de atraso."""
    def __init__(self, interval: float, out_path: str, target_pid: int = None):
        self.interval = max(0.1, interval)
        self.out_path = out_path
        self.target_pid = target_pid
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
                proc = psutil.Process(self.target_pid)
                proc.cpu_percent(interval=None)
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
            fieldnames += ["proc_rss_mib"]

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
                    try:
                        row["proc_rss_mib"] = round(proc.memory_info().rss / (1024 ** 2), 2)
                    except Exception:
                        row["proc_rss_mib"] = ""
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
    workload_url = (
        f"{target_clean}/process"
        f"?size_mb={args.size_mib}&iterations={args.iterations}"
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
    monitor = SystemMetricsCollector(
        interval=1.0,
        out_path=sys_csv,
        target_pid=server_proc.pid if server_proc else None,
    )
    monitor.start()

    print(
        f"[exp] Iniciando carga: {args.vus} VUs × {args.duration}s | "
        f"size_mib={args.size_mib} iterations={args.iterations}"
    )
    t0 = time.perf_counter()
    results = run_load(workload_url, args.vus, args.duration)
    elapsed = time.perf_counter() - t0

    monitor.stop()

    summary = summarize(results)
    rps = round(summary["total_requests"] / elapsed, 2) if elapsed > 0 else 0.0
    summary["measured_rps"] = rps
    summary["test_duration_s"] = round(elapsed, 2)

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
