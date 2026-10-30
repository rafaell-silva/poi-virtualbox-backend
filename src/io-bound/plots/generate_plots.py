#!/usr/bin/env python3
"""
Gerador de gráficos a partir dos dados coletados.

Lê os CSVs em data/results/ e produz duas figuras agrupadas:
  1. overview_demand.png — demanda × latência, vazão, CPU, memória,
     I/O e eficiência (eixos fixos por métrica);
  2. overview_time.png — CPU/memória do sistema e do processo ao longo do tempo.

Os gráficos "ao longo do tempo" combinam todas as amostras em uma única série
ordenada pelo instante real de coleta (timestamp_utc), de modo que os níveis de
demanda apareçam distribuídos ao longo do eixo, na ordem em que ocorreram.

Uso:
    python plots/generate_plots.py
    python plots/generate_plots.py --results-dir data/results --out-dir plots/output

    # listar run_ids e metadados disponíveis (não gera gráficos):
    python plots/generate_plots.py --list-runs

    # selecionar uma configuração e ignorar CSVs legacy:
    python plots/generate_plots.py --exclude-legacy --workers 8 --request-mix mixed

IMPORTANTE:
    Os gráficos são gerados a partir de medições reais.  Se não houver dados
    suficientes, o script informa quais arquivos estão faltando e termina sem
    gerar gráficos com dados fictícios.
"""

import argparse
import csv
import datetime
import os
import sys
import glob
import math
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--results-dir", default="data/results")
    p.add_argument("--out-dir", default="plots/output")
    p.add_argument("--show", action="store_true", help="Exibir gráficos na tela.")
    p.add_argument("--scenario", default="", help="Filtra por cenário (lista separada por vírgula).")
    p.add_argument("--workers", default="", help="Filtra por nº de workers (lista separada por vírgula).")
    p.add_argument("--request-mix", default="", help="Filtra por mix: uniform,mixed (lista separada por vírgula).")
    p.add_argument("--rep", default="", help="Filtra por repetição (lista separada por vírgula).")
    p.add_argument("--run-id", default="", help="Filtra por run_id exato (lista separada por vírgula).")
    p.add_argument("--exclude-legacy", action="store_true", help="Ignora CSVs sem run_id (legacy).")
    p.add_argument("--list-runs", action="store_true", help="Lista run_ids/metadados e não gera gráficos.")
    return p.parse_args()


# --------------------------------------------------------------------------- #
# Leitura dos CSVs
# --------------------------------------------------------------------------- #

def read_experiment_csv(path: str) -> dict:
    """Lê um CSV de experimento (formato metric,value) e retorna um dict."""
    data = {}
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            data[row["metric"]] = row["value"]
    return data


def read_sys_csv(path: str) -> list[dict]:
    """Lê um CSV de métricas do sistema (colunas time-series) e retorna lista de dicts."""
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def load_all_experiments(results_dir: str):
    """
    Carrega todos os arquivos de experimento (exp_*.csv e k6_*.csv) e
    sys_*.csv correlacionados pelo run_id (label/timestamp só para CSVs legacy).
    """
    exp_files = sorted(glob.glob(os.path.join(results_dir, "exp_*.csv")))
    k6_files = sorted(glob.glob(os.path.join(results_dir, "k6_*.csv")))
    sys_files = sorted(glob.glob(os.path.join(results_dir, "sys_*.csv")))

    experiments = []
    for f in exp_files + k6_files:
        try:
            d = read_experiment_csv(f)
            d["_source_file"] = f
            experiments.append(d)
        except Exception as e:
            print(f"[warn] Ignorando {f}: {e}")

    sys_data = {}
    for f in sys_files:
        try:
            rows = read_sys_csv(f)
            sys_data[f] = rows
        except Exception as e:
            print(f"[warn] Ignorando {f}: {e}")

    return experiments, sys_data


def _split_filter(value: str) -> set:
    return {v.strip() for v in (value or "").split(",") if v.strip()}


def _exp_value(exp: dict, key: str) -> str:
    return (exp.get(key) or "").strip()


def _meta_matches(record: dict, scenarios: set, workers: set, mixes: set,
                  reps: set, run_ids: set) -> bool:
    run_id = (record.get("run_id") or "").strip()
    if not run_id:
        return False
    if scenarios and (record.get("scenario") or "").strip() not in scenarios:
        return False
    if workers and (record.get("workers") or "").strip() not in workers:
        return False
    if mixes and (record.get("request_mix") or "").strip() not in mixes:
        return False
    if reps and (record.get("rep") or "").strip() not in reps:
        return False
    if run_ids and run_id not in run_ids:
        return False
    return True


def _sys_meta_row(rows: list):
    for row in rows:
        if (row.get("run_id") or "").strip():
            return row
    return None


def filter_experiments(experiments: list, scenario: str = "", workers: str = "",
                       request_mix: str = "", rep: str = "", run_id: str = "",
                       exclude_legacy: bool = False) -> list:
    """
    Filtra experimentos pelos metadados gravados no CSV.

    CSVs legacy (sem run_id) só entram quando nenhum filtro é pedido e
    --exclude-legacy não foi usado. Ao comparar workers/mix/reps, os dados
    antigos não contaminam a figura.
    """
    scenarios = _split_filter(scenario)
    worker_set = _split_filter(workers)
    mixes = _split_filter(request_mix)
    reps = _split_filter(rep)
    run_ids = _split_filter(run_id)
    any_filter = bool(scenarios or worker_set or mixes or reps or run_ids)

    selected = []
    for exp in experiments:
        if not _exp_value(exp, "run_id"):
            if not exclude_legacy and not any_filter:
                selected.append(exp)
            continue
        if _meta_matches(exp, scenarios, worker_set, mixes, reps, run_ids):
            selected.append(exp)
    return selected


def filter_sys_data(sys_data: dict, scenario: str = "", workers: str = "",
                    request_mix: str = "", rep: str = "", run_id: str = "",
                    exclude_legacy: bool = False) -> dict:
    """Filtra os CSVs de sistema com os mesmos critérios dos experimentos."""
    scenarios = _split_filter(scenario)
    worker_set = _split_filter(workers)
    mixes = _split_filter(request_mix)
    reps = _split_filter(rep)
    run_ids = _split_filter(run_id)
    any_filter = bool(scenarios or worker_set or mixes or reps or run_ids)

    if not any_filter and not exclude_legacy:
        return sys_data

    selected = {}
    for path, rows in sys_data.items():
        meta = _sys_meta_row(rows)
        if meta is None:
            continue
        if _meta_matches(meta, scenarios, worker_set, mixes, reps, run_ids):
            selected[path] = rows
    return selected


def describe_filters(args) -> str:
    parts = []
    if args.scenario:
        parts.append(f"scenario={args.scenario}")
    if args.workers:
        parts.append(f"workers={args.workers}")
    if args.request_mix:
        parts.append(f"request_mix={args.request_mix}")
    if args.rep:
        parts.append(f"rep={args.rep}")
    if args.run_id:
        parts.append(f"run_id={args.run_id}")
    if args.exclude_legacy:
        parts.append("exclude_legacy")
    return ", ".join(parts) if parts else "nenhum"


def list_runs(experiments: list):
    """Imprime run_ids e metadados para o usuário montar os filtros."""
    print("run_id\tscenario\tworkers\trequest_mix\trep\tvus\tarquivo")
    for exp in sorted(experiments, key=lambda e: e.get("_source_file", "")):
        run_id = _exp_value(exp, "run_id") or "(legacy)"
        print(
            "\t".join([
                run_id,
                _exp_value(exp, "scenario") or "-",
                _exp_value(exp, "workers") or "-",
                _exp_value(exp, "request_mix") or "-",
                _exp_value(exp, "rep") or "-",
                _exp_value(exp, "vus") or _exp_value(exp, "vus_max") or "-",
                exp.get("_source_file", ""),
            ])
        )


def float_or_none(v):
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_iso_ts(v):
    """Converte 'timestamp_utc' (ISO-8601) em datetime com fuso; None se inválido."""
    if not v:
        return None
    try:
        return datetime.datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None


def _experiment_run_id(exp: dict):
    """run_id do experimento; None em CSVs legacy (anteriores ao run_id)."""
    value = (exp.get("run_id") or "").strip()
    return value or None


def _sys_file_run_id(rows: list):
    """run_id declarado em um CSV de sistema (primeira linha que o tiver)."""
    for row in rows:
        value = (row.get("run_id") or "").strip()
        if value:
            return value
    return None


def build_sys_run_index(sys_data: dict):
    """
    Indexa run_id → arquivo sys_* para join exato com o experimento.

    Esse é o caminho preferencial: o run_id é gerado uma única vez pelo
    orquestrador e gravado tanto no resumo (exp_*/k6_*) quanto no CSV de
    sistema. CSVs legacy (sem a coluna run_id) ficam fora do índice e
    continuam usando a heurística antiga de nome/timestamp/VUs.
    """
    index = {}
    for path, rows in sys_data.items():
        run_id = _sys_file_run_id(rows)
        if not run_id:
            continue
        current = index.get(run_id)
        if current is None or len(rows) > len(sys_data[current]):
            if current is not None:
                print(
                    f"[plots][aviso] run_id '{run_id}' repetido em sys_*; "
                    f"usando {os.path.basename(path)}"
                )
            index[run_id] = path
    return index


def classify_sys_files(experiments: list, sys_series: dict, sys_data: dict = None):
    """
    Separa os arquivos sys_* em:
      - level_files: correspondem a um experimento (rodada por nível de demanda);
      - monitor_files: coletas contínuas (sem experimento associado).

    Com run_id disponível, a classificação é exata. Para CSVs legacy, cai na
    heurística antiga: o nome-base do experimento (sem prefixo exp_/k6_) deve
    estar contido no nome do arquivo sys_.
    """
    level_run_ids = {rid for e in experiments if (rid := _experiment_run_id(e))}
    legacy_cores = set()
    for e in experiments:
        src = e.get("_source_file", "")
        if src:
            legacy_cores.add(Path(src).stem.replace("exp_", "").replace("k6_", ""))

    level_files, monitor_files = [], []
    for p in sys_series:
        run_id = _sys_file_run_id(sys_data.get(p, [])) if sys_data else None
        if run_id:
            is_level = run_id in level_run_ids
        else:
            stem = Path(p).stem
            is_level = any(c and c in stem for c in legacy_cores)
        (level_files if is_level else monitor_files).append(p)
    return level_files, monitor_files


GAP_BREAK_S = 20.0  # vãos maiores que isto quebram a linha (ex.: entre sessões)


def combine_on_timeline(sys_series: dict, key: str):
    """
    Combina as amostras de todos os arquivos em uma única série ordenada pelo
    instante REAL de coleta (timestamp_utc), retornando segundos decorridos desde
    a primeira amostra global. Assim os níveis aparecem distribuídos ao longo do
    eixo temporal, na ordem em que ocorreram, em vez de sobrepostos em t=0.

    Vãos maiores que GAP_BREAK_S (ex.: entre execuções/sessões distintas) inserem
    um ponto nulo, de modo que a linha não cruze o intervalo vazio artificialmente.
    """
    pts = []
    for series in sys_series.values():
        tabs = series.get("t_abs") or [None] * len(series["elapsed"])
        for t, v in zip(tabs, series[key]):
            if t is not None and v is not None:
                pts.append((t, v))
    if not pts:
        return [], []
    t0 = min(t for t, _ in pts)
    pts.sort(key=lambda p: p[0])

    xs, ys = [], []
    prev = None
    for t, v in pts:
        off = (t - t0).total_seconds()
        if prev is not None and (off - prev) > GAP_BREAK_S:
            xs.append(off)
            ys.append(None)
        xs.append(off)
        ys.append(v)
        prev = off
    return xs, ys


def _match_sys_file(exp: dict, sys_data: dict, sys_run_index: dict = None):
    """
    Encontra o arquivo sys_* correspondente ao experimento exp_*/k6_*.

    Caminho preferencial: match exato pelo run_id (gerado pelo orquestrador e
    presente nos dois CSVs). Fallbacks para dados legacy: substring de
    nome/timestamp e, por último, quantidade de VUs.
    """
    if sys_run_index:
        run_id = _experiment_run_id(exp)
        if run_id and run_id in sys_run_index:
            return sys_run_index[run_id]

    label = exp.get("_source_file", "")
    if not label:
        return None
    core = Path(label).stem.replace("exp_", "").replace("k6_", "")
    candidates = [p for p in sys_data if core in Path(p).stem]
    if candidates:
        return sorted(candidates, key=len, reverse=True)[0]

    ts = exp.get("timestamp_utc", "")
    token = ts.replace("-", "").replace(":", "").replace(".", "")[:13]
    for p in sys_data:
        stem = Path(p).stem.replace(":", "").replace("-", "")
        if token and token in stem:
            candidates.append(p)
    if candidates:
        return sorted(candidates, key=len, reverse=True)[0]

    vus = exp.get("vus") or exp.get("vus_max")
    if vus is not None:
        pattern = f"_{int(float(vus))}vus"
        vu_candidates = [p for p in sys_data if pattern in Path(p).stem]
        if vu_candidates:
            return sorted(vu_candidates)[-1]

    return None


def _series_mean(rows: list, keys: list):
    vals = []
    for r in rows:
        for k in keys:
            v = float_or_none(r.get(k))
            if v is not None:
                vals.append(v)
                break
    return (sum(vals) / len(vals)) if vals else None


# --------------------------------------------------------------------------- #
# Extração de séries para gráficos
# --------------------------------------------------------------------------- #

def extract_latency_series(experiments: list):
    """
    Retorna listas paralelas: demanda, lat_avg, lat_p90, lat_p95.
    Demanda = vus (experimentos locais) ou vus_max (k6).
    """
    points = []
    for e in experiments:
        demand = float_or_none(e.get("vus") or e.get("vus_max"))
        lat_avg = float_or_none(
            e.get("latency_avg_ms") or e.get("http_req_duration_avg_ms")
        )
        lat_p90 = float_or_none(
            e.get("latency_p90_ms") or e.get("http_req_duration_p90_ms")
        )
        lat_p95 = float_or_none(
            e.get("latency_p95_ms") or e.get("http_req_duration_p95_ms")
        )
        if demand is not None and lat_avg is not None:
            sz = e.get("size_mib") or e.get("size_mb", "?")
            points.append((demand, lat_avg, lat_p90, lat_p95,
                           sz, e.get("_source_file", "")))
    return sorted(points, key=lambda x: x[0])


def extract_rps_series(experiments: list):
    points = []
    for e in experiments:
        demand = float_or_none(e.get("vus") or e.get("vus_max"))
        rps = float_or_none(
            e.get("measured_rps") or e.get("http_reqs_per_second")
        )
        if demand is not None and rps is not None:
            sz = e.get("size_mib") or e.get("size_mb", "?")
            points.append((demand, rps, sz))
    return sorted(points, key=lambda x: x[0])


def extract_sys_series(sys_data: dict):
    """
    Para cada arquivo sys_*, extrai séries temporais de CPU, memória, processo e disco.
    Retorna dict: filename → {elapsed, cpu, mem, proc_cpu, proc_rss, disk_read, disk_write}
    com listas alinhadas (valores ausentes viram None).
    """
    result = {}
    for path, rows in sys_data.items():
        s = {
            "elapsed": [], "t_abs": [], "cpu": [], "mem": [],
            "proc_cpu": [], "proc_rss": [],
            "disk_read": [], "disk_write": [],
        }
        for row in rows:
            e = float_or_none(row.get("elapsed_s"))
            if e is None:
                continue
            s["elapsed"].append(e)
            s["t_abs"].append(parse_iso_ts(row.get("timestamp_utc")))
            s["cpu"].append(float_or_none(row.get("cpu_total_pct") or row.get("cpu_percent_total")))
            s["mem"].append(float_or_none(row.get("mem_percent")))
            s["proc_cpu"].append(float_or_none(row.get("proc_cpu_percent")))
            s["proc_rss"].append(float_or_none(row.get("proc_rss_mib")))
            s["disk_read"].append(float_or_none(row.get("disk_read_mib_s")))
            s["disk_write"].append(float_or_none(row.get("disk_write_mib_s")))
        if s["elapsed"]:
            result[path] = s
    return result


def _pairs(xs, ys):
    return [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]


# --------------------------------------------------------------------------- #
# Geração dos gráficos
# --------------------------------------------------------------------------- #

def plot_latency(points, out_dir: str, show: bool):
    import matplotlib.pyplot as plt

    if not points:
        print("[plots] Sem dados de latência para plotar.")
        return

    demands = [p[0] for p in points]
    avgs = [p[1] for p in points]
    p90s = [p[2] if p[2] is not None else float("nan") for p in points]
    p95s = [p[3] if p[3] is not None else float("nan") for p in points]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(demands, avgs, "o-", label="Avg", linewidth=2)
    ax.plot(demands, p90s, "s--", label="p90", linewidth=1.5)
    ax.plot(demands, p95s, "^-.", label="p95", linewidth=1.5)
    ax.set_xlabel("Demanda (VUs / usuários virtuais)")
    ax.set_ylabel("Latência (ms)")
    ax.set_title("Demanda × Latência das requisições")
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_path = os.path.join(out_dir, "latency_vs_demand.png")
    fig.savefig(out_path, dpi=150)
    print(f"[plots] → {out_path}")
    if show:
        plt.show()
    plt.close(fig)


def plot_rps(points, out_dir: str, show: bool):
    import matplotlib.pyplot as plt

    if not points:
        print("[plots] Sem dados de RPS para plotar.")
        return

    demands = [p[0] for p in points]
    rps_vals = [p[1] for p in points]

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.bar(demands, rps_vals, width=max(demands) * 0.05 if max(demands) > 0 else 1,
           color="steelblue", alpha=0.8)
    ax.plot(demands, rps_vals, "o-", color="navy", linewidth=1.5)
    ax.set_xlabel("Demanda (VUs)")
    ax.set_ylabel("Requisições por segundo (RPS)")
    ax.set_title("Demanda × Throughput")
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()

    out_path = os.path.join(out_dir, "rps_vs_demand.png")
    fig.savefig(out_path, dpi=150)
    print(f"[plots] → {out_path}")
    if show:
        plt.show()
    plt.close(fig)


def _plot_over_time(sys_series: dict, out_dir: str, show: bool, *,
                    key: str, ylabel: str, title: str, filename: str,
                    ylim=None):
    import matplotlib.pyplot as plt

    usable = {p: s for p, s in sys_series.items() if any(v is not None for v in s[key])}
    if not usable:
        print(f"[plots] Sem dados de '{key}' para plotar '{filename}'.")
        return

    fig, ax = plt.subplots(figsize=(10, 5))

    xs, ys = combine_on_timeline(usable, key)
    if xs:
        ax.plot(xs, ys, linewidth=1.4, color="tab:blue")
        ax.set_xlabel("Tempo real decorrido desde o início do experimento (s)")
    else:
        # Fallback quando não há timestamp_utc: plota cada arquivo separadamente.
        for path, series in usable.items():
            xy = _pairs(series["elapsed"], series[key])
            if xy:
                px, py = zip(*xy)
                ax.plot(px, py, label=Path(path).stem, linewidth=1.2)
        ax.set_xlabel("Tempo decorrido (s)")
        ax.legend(fontsize=7)

    ax.set_ylabel(ylabel)
    ax.set_title(title)
    if ylim is not None:
        ax.set_ylim(*ylim)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_path = os.path.join(out_dir, filename)
    fig.savefig(out_path, dpi=150)
    print(f"[plots] → {out_path}")
    if show:
        plt.show()
    plt.close(fig)


def plot_proc_resources(sys_series: dict, out_dir: str, show: bool):
    """CPU% e RSS (MiB) do processo alvo ao longo do tempo (se --target-pid foi usado)."""
    import matplotlib.pyplot as plt

    usable = {
        p: s for p, s in sys_series.items()
        if any(v is not None for v in s["proc_cpu"]) or any(v is not None for v in s["proc_rss"])
    }
    if not usable:
        print("[plots] Sem dados de processo (proc_cpu_percent/proc_rss_mib). "
              "Execute o monitor com --target-pid para habilitar.")
        return

    fig, ax1 = plt.subplots(figsize=(10, 5))
    ax2 = ax1.twinx()

    xc, yc = combine_on_timeline(usable, "proc_cpu")
    xr, yr = combine_on_timeline(usable, "proc_rss")
    if xc:
        ax1.plot(xc, yc, "-", color="tab:red", label="CPU do processo", linewidth=1.4)
    if xr:
        ax2.plot(xr, yr, "--", color="tab:blue", label="RSS do processo", linewidth=1.4)

    if not xc and not xr:
        for path, series in usable.items():
            xy = _pairs(series["elapsed"], series["proc_cpu"])
            if xy:
                px, py = zip(*xy)
                ax1.plot(px, py, label=f"{Path(path).stem} CPU", linewidth=1.2)
            xy = _pairs(series["elapsed"], series["proc_rss"])
            if xy:
                px, py = zip(*xy)
                ax2.plot(px, py, "--", label=f"{Path(path).stem} RSS", linewidth=1.2)

    ax1.set_xlabel("Tempo real decorrido desde o início do experimento (s)")
    ax1.set_ylabel("CPU do processo (%)")
    ax2.set_ylabel("RSS do processo (MiB)")
    ax1.set_title("Recursos do processo alvo ao longo do tempo")
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, fontsize=8, loc="upper left")
    ax1.grid(True, alpha=0.3)
    fig.tight_layout()

    out_path = os.path.join(out_dir, "proc_resources_over_time.png")
    fig.savefig(out_path, dpi=150)
    print(f"[plots] → {out_path}")
    if show:
        plt.show()
    plt.close(fig)


def plot_disk(sys_series: dict, out_dir: str, show: bool):
    """Taxas de leitura/escrita de disco (MiB/s) ao longo do tempo."""
    import matplotlib.pyplot as plt

    usable = {
        p: s for p, s in sys_series.items()
        if any(v is not None for v in s["disk_read"]) or any(v is not None for v in s["disk_write"])
    }
    if not usable:
        print("[plots] Sem dados de disco (disk_read_mib_s/disk_write_mib_s).")
        return

    fig, ax = plt.subplots(figsize=(10, 5))

    xr, yr = combine_on_timeline(usable, "disk_read")
    xw, yw = combine_on_timeline(usable, "disk_write")
    if xr:
        ax.plot(xr, yr, "-", color="tab:blue", label="Leitura", linewidth=1.4)
    if xw:
        ax.plot(xw, yw, "--", color="tab:orange", label="Escrita", linewidth=1.4)

    if not xr and not xw:
        for path, series in usable.items():
            xy = _pairs(series["elapsed"], series["disk_read"])
            if xy:
                px, py = zip(*xy)
                ax.plot(px, py, "-", label=f"{Path(path).stem} read", linewidth=1.2)
            xy = _pairs(series["elapsed"], series["disk_write"])
            if xy:
                px, py = zip(*xy)
                ax.plot(px, py, "--", label=f"{Path(path).stem} write", linewidth=1.2)

    ax.set_xlabel("Tempo real decorrido desde o início do experimento (s)")
    ax.set_ylabel("Taxa de I/O de disco (MiB/s)")
    ax.set_title("I/O de disco ao longo do tempo")
    ax.legend(fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    out_path = os.path.join(out_dir, "disk_io_over_time.png")
    fig.savefig(out_path, dpi=150)
    print(f"[plots] → {out_path}")
    if show:
        plt.show()
    plt.close(fig)


def _demand_resource_points(experiments: list, sys_data: dict, sys_run_index: dict = None):
    """
    Correlaciona cada experimento com o arquivo sys_* correspondente e extrai
    as médias de CPU (processo, se houver; senão do sistema) e de memória
    (RSS do processo, se houver; senão % do sistema).
    """
    points = []
    for e in experiments:
        demand = float_or_none(e.get("vus") or e.get("vus_max"))
        if demand is None:
            continue
        matched = _match_sys_file(e, sys_data, sys_run_index)
        if not matched:
            continue
        rows = sys_data[matched]
        cpu = _series_mean(rows, ["proc_cpu_percent"]) 
        if cpu is None:
            cpu = _series_mean(rows, ["cpu_total_pct", "cpu_percent_total"])
        mem = _series_mean(rows, ["proc_rss_mib"])
        mem_pct = _series_mean(rows, ["mem_percent"])
        disk_w = _series_mean(rows, ["disk_write_mib_s"])
        disk_r = _series_mean(rows, ["disk_read_mib_s"])
        if cpu is not None:
            points.append({
                "demand": demand, "cpu": cpu, "mem": mem, "mem_pct": mem_pct,
                "disk_read": disk_r, "disk_write": disk_w,
            })
    return sorted(points, key=lambda p: p["demand"])


def plot_demand_vs_resources(experiments: list, sys_data: dict, out_dir: str, show: bool,
                             sys_run_index: dict = None):
    """
    Gera painéis Demanda × CPU, Demanda × Memória e, quando disponível,
    Demanda × I/O de disco. Requer ao menos 2 pontos para produzir o gráfico.
    """
    import matplotlib.pyplot as plt

    points = _demand_resource_points(experiments, sys_data, sys_run_index)
    if len(points) < 2:
        print("[plots] Dados insuficientes para gráfico Demanda×Recursos (precisa ≥2 experimentos).")
        return

    demands = [p["demand"] for p in points]
    cpus = [p["cpu"] for p in points]
    mems = [p["mem"] if p["mem"] is not None else p["mem_pct"] for p in points]
    mem_is_pct = all(p["mem"] is None for p in points)
    disks_w = [p["disk_write"] for p in points]
    has_disk = any(v is not None for v in disks_w)

    panels = 2 + (1 if has_disk else 0)
    fig, axes = plt.subplots(1, panels, figsize=(6 * panels, 5))
    if panels == 1:
        axes = [axes]

    axes[0].plot(demands, cpus, "o-r", linewidth=2)
    axes[0].set_xlabel("Demanda (VUs)")
    axes[0].set_ylabel("CPU média (%)")
    axes[0].set_title("Demanda × CPU")
    axes[0].set_ylim(0, 105)
    axes[0].grid(True, alpha=0.3)

    mem_vals = [m if m is not None else float("nan") for m in mems]
    axes[1].plot(demands, mem_vals, "o-b", linewidth=2)
    axes[1].set_xlabel("Demanda (VUs)")
    axes[1].set_ylabel("Memória usada (%)" if mem_is_pct else "RSS do processo (MiB)")
    axes[1].set_title("Demanda × Memória")
    if mem_is_pct:
        axes[1].set_ylim(0, 105)
    axes[1].grid(True, alpha=0.3)

    if has_disk:
        dw = [v if v is not None else float("nan") for v in disks_w]
        axes[2].plot(demands, dw, "o-g", linewidth=2)
        axes[2].set_xlabel("Demanda (VUs)")
        axes[2].set_ylabel("Escrita em disco (MiB/s)")
        axes[2].set_title("Demanda × I/O de disco")
        axes[2].grid(True, alpha=0.3)

    fig.suptitle("Recursos vs. Demanda", fontsize=13)
    fig.tight_layout()

    out_path = os.path.join(out_dir, "demand_vs_resources.png")
    fig.savefig(out_path, dpi=150)
    print(f"[plots] → {out_path}")
    if show:
        plt.show()
    plt.close(fig)


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def _finite(values):
    """Remove None/NaN/Inf para calcular limites de eixo sem quebrar o matplotlib."""
    return [v for v in values if v is not None and math.isfinite(v)]


def plot_overview(experiments, sys_data, out_dir, show, sys_run_index=None):
    """
    Figura única com os painéis relevantes do perfil I/O: latência, vazão,
    CPU, memória, I/O de disco e duração de escrita/fsync em função da demanda.

    Os eixos são fixados no máximo global de cada métrica para permitir
    comparação visual entre níveis e execuções.
    """
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator

    lat = extract_latency_series(experiments)
    rps = extract_rps_series(experiments)
    res = _demand_resource_points(experiments, sys_data, sys_run_index)

    demands = _finite(
        [p[0] for p in lat] + [p[0] for p in rps] + [p["demand"] for p in res]
    )
    if not demands:
        print("[plots] Sem dados para a visão geral demanda × recursos.")
        return
    xmax = max(max(demands) * 1.10, 1.0)

    fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharex=True)
    ax_lat, ax_rps, ax_cpu, ax_mem, ax_io, ax_app = axes.ravel()

    if lat:
        ax_lat.plot([p[0] for p in lat], [p[1] for p in lat], "o-", label="média")
        ax_lat.plot([p[0] for p in lat], [p[2] for p in lat], "s--", label="p90")
        ax_lat.plot([p[0] for p in lat], [p[3] for p in lat], "^-", label="p95")
        ymax = max(_finite([p[1] for p in lat] + [p[2] for p in lat] + [p[3] for p in lat]))
        ax_lat.set_ylim(0, max(ymax * 1.15, 1.0))
        ax_lat.legend(fontsize=8)
    ax_lat.set_title("Latência × Demanda")
    ax_lat.set_ylabel("ms")

    if rps:
        ax_rps.plot([p[0] for p in rps], [p[1] for p in rps], "o-", color="tab:green")
        ax_rps.set_ylim(0, max(max(_finite([p[1] for p in rps])) * 1.15, 1.0))
    ax_rps.set_title("Vazão × Demanda")
    ax_rps.set_ylabel("req/s")

    cpu_vals = _finite([p["cpu"] for p in res])
    if cpu_vals:
        ax_cpu.plot([p["demand"] for p in res], [p["cpu"] for p in res], "o-", color="tab:red")
        ax_cpu.set_ylim(0, max(max(cpu_vals) * 1.15, 1.0))
    ax_cpu.set_title("CPU × Demanda")
    ax_cpu.set_ylabel("% de CPU")

    if res and all(p["mem"] is not None for p in res):
        mem_vals = [p["mem"] for p in res]
        mem_label = "RSS do processo (MiB)"
    elif res and all(p["mem_pct"] is not None for p in res):
        mem_vals = [p["mem_pct"] for p in res]
        mem_label = "Memória do sistema (%)"
    else:
        mem_vals = []
        mem_label = "Memória"
    if mem_vals:
        ax_mem.plot([p["demand"] for p in res], mem_vals, "o-", color="tab:purple")
        ax_mem.set_ylim(0, max(max(_finite(mem_vals)) * 1.15, 1.0))
    ax_mem.set_title("Memória × Demanda")
    ax_mem.set_ylabel(mem_label)

    if res:
        dw = [p["disk_write"] for p in res]
        dr = [p["disk_read"] for p in res]
        if any(v is not None for v in dw):
            ax_io.plot([p["demand"] for p in res],
                       [v if v is not None else float("nan") for v in dw],
                       "o-", label="escrita")
        if any(v is not None for v in dr):
            ax_io.plot([p["demand"] for p in res],
                       [v if v is not None else float("nan") for v in dr],
                       "s--", label="leitura")
        io_vals = _finite(dw + dr)
        if io_vals:
            ax_io.set_ylim(0, max(max(io_vals) * 1.15, 1.0))
        if any(v is not None for v in dw + dr):
            ax_io.legend(fontsize=8)
    ax_io.set_title("I/O de disco × Demanda")
    ax_io.set_ylabel("MiB/s")

    # Duração do trabalho de I/O reportada pela própria aplicação/k6.
    app = []
    for e in experiments:
        d = float_or_none(e.get("vus") or e.get("vus_max"))
        w = float_or_none(e.get("app_write_duration_avg_ms"))
        f = float_or_none(e.get("app_fsync_duration_avg_ms"))
        if d is not None and (w is not None or f is not None):
            app.append((d, w, f))
    app.sort(key=lambda x: x[0])
    if app:
        if any(p[1] is not None for p in app):
            ax_app.plot([p[0] for p in app],
                        [p[1] if p[1] is not None else float("nan") for p in app],
                        "o-", label="escrita")
        if any(p[2] is not None for p in app):
            ax_app.plot([p[0] for p in app],
                        [p[2] if p[2] is not None else float("nan") for p in app],
                        "s--", label="fsync")
        app_vals = _finite([p[1] for p in app] + [p[2] for p in app])
        if app_vals:
            ax_app.set_ylim(0, max(max(app_vals) * 1.15, 1.0))
        ax_app.legend(fontsize=8)
    ax_app.set_title("Trabalho de I/O do app × Demanda")
    ax_app.set_ylabel("ms")

    for ax in axes.ravel():
        ax.set_xlim(0, xmax)
        ax.grid(alpha=0.3)
        ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    for ax in axes[-1]:
        ax.set_xlabel("VUs (demanda)")

    fig.suptitle("Visão geral: demanda × latência, vazão e I/O", fontsize=13)
    fig.tight_layout()
    path = os.path.join(out_dir, "overview_demand.png")
    fig.savefig(path, dpi=140, bbox_inches="tight")
    print(f"[plots] → {path}")
    if show:
        plt.show()
    plt.close(fig)


def plot_overview_time(sys_series, out_dir, show):
    """Figura única com CPU/memória do sistema e do processo ao longo do tempo."""
    import matplotlib.pyplot as plt

    if not sys_series:
        print("[plots] Sem séries temporais para a visão geral.")
        return

    specs = [
        ("cpu", "CPU total do sistema", "%"),
        ("mem", "Memória do sistema", "%"),
        ("proc_cpu", "CPU do processo", "%"),
        ("proc_rss", "RSS do processo", "MiB"),
    ]
    fig, axes = plt.subplots(2, 2, figsize=(14, 8), sharex=True)
    any_data = False
    xmax = 0.0
    for ax, (key, title, unit) in zip(axes.ravel(), specs):
        xs, ys = combine_on_timeline(sys_series, key)
        if xs:
            any_data = True
            xmax = max(xmax, max(xs))
            ax.plot(xs, [v if v is not None else float("nan") for v in ys], lw=1.2)
            vals = _finite(ys)
            if vals:
                ax.set_ylim(0, max(max(vals) * 1.15, 1.0))
        ax.set_title(title)
        ax.set_ylabel(unit)
        ax.grid(alpha=0.3)
    if not any_data:
        plt.close(fig)
        print("[plots] Sem amostras temporais para a visão geral.")
        return
    for ax in axes[-1]:
        ax.set_xlabel("tempo desde a primeira amostra (s)")
    for ax in axes.ravel():
        ax.set_xlim(0, max(xmax * 1.02, 1.0))
    fig.suptitle("Visão geral: recursos ao longo do tempo", fontsize=13)
    fig.tight_layout()
    path = os.path.join(out_dir, "overview_time.png")
    fig.savefig(path, dpi=140, bbox_inches="tight")
    print(f"[plots] → {path}")
    if show:
        plt.show()
    plt.close(fig)


def main():
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    experiments, sys_data = load_all_experiments(args.results_dir)

    if args.list_runs:
        list_runs(experiments)
        return

    experiments = filter_experiments(
        experiments,
        scenario=args.scenario,
        workers=args.workers,
        request_mix=args.request_mix,
        rep=args.rep,
        run_id=args.run_id,
        exclude_legacy=args.exclude_legacy,
    )
    sys_data = filter_sys_data(
        sys_data,
        scenario=args.scenario,
        workers=args.workers,
        request_mix=args.request_mix,
        rep=args.rep,
        run_id=args.run_id,
        exclude_legacy=args.exclude_legacy,
    )

    if describe_filters(args) != "nenhum":
        print(
            f"[plots] Filtros: {describe_filters(args)} | "
            f"{len(experiments)} experimento(s), {len(sys_data)} sistema(s)."
        )

    if not experiments and not sys_data:
        if describe_filters(args) != "nenhum":
            print(
                f"[plots] Nenhum dado corresponde aos filtros "
                f"({describe_filters(args)}) em '{args.results_dir}'."
            )
            sys.exit(0)
        print(
            f"[plots] Nenhum dado encontrado em '{args.results_dir}'.\n"
            "        Execute primeiro os experimentos com:\n"
            "          python metrics/collectors/run_experiment.py --start-server ...\n"
            "        ou com k6:\n"
            "          k6 run k6/load_ramp.js"
        )
        sys.exit(0)

    print(f"[plots] {len(experiments)} arquivo(s) de experimento | "
          f"{len(sys_data)} arquivo(s) de sistema encontrados.")

    lat_points = extract_latency_series(experiments)
    rps_points = extract_rps_series(experiments)
    sys_series = extract_sys_series(sys_data)

    # Para os gráficos "ao longo do tempo", dá-se preferência aos arquivos por
    # nível de demanda (correlacionados a um experimento). Se não houver, usa-se
    # tudo. As amostras são unidas no tempo real de coleta (ver combine_on_timeline).
    # Índice run_id -> sys_* para join exato (legacy cai na heurística antiga).
    sys_run_index = build_sys_run_index(sys_data)
    level_paths, monitor_paths = classify_sys_files(experiments, sys_series, sys_data)
    res_paths = level_paths if level_paths else list(sys_series)
    proc_paths = monitor_paths if monitor_paths else list(sys_series)
    res_series = {p: sys_series[p] for p in res_paths}
    proc_series = {p: sys_series[p] for p in proc_paths}

    # Série temporal agrupada: níveis + monitor contínuo como complemento.
    time_series = dict(res_series)
    for p, s in proc_series.items():
        time_series.setdefault(p, s)

    plot_overview(experiments, sys_data, args.out_dir, args.show, sys_run_index)
    plot_overview_time(time_series, args.out_dir, args.show)

    print(f"\n[plots] Concluído. Arquivos em '{args.out_dir}/'.")


if __name__ == "__main__":
    main()
