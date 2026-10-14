#!/usr/bin/env python3
"""
Coletor de métricas do sistema operacional (CPU, Memória e Swap).

Finalidade e Metodologia:
-------------------------
Coleta periodicamente amostras de utilização de recursos do hospedeiro/sistema:
- CPU: porcentagem total e discriminação por núcleo lógico.
- Memória RAM: total, usada, disponível (MiB) e porcentagem de uso.
- Swap: total, usado (MiB), porcentagem e taxas diferenciais de paginação
  (swap-in e swap-out em KiB/s), essenciais para investigar trashing de memória.
- Disco: taxas diferenciais de leitura/escrita (MiB/s), IOPS de leitura/escrita e
  percentual de tempo ocupado (busy), essenciais para caracterizar cargas IO-bound.
- Processo alvo (opcional): RSS (MiB), CPU (%) e taxas de I/O (KiB/s) de um PID.

IMPORTANTE — Rastreabilidade e Local de Execução:
-------------------------------------------------
O parâmetro `--machine [local|vm]` é estritamente uma ETIQUETA DE METADADOS para
identificação e nomenclatura do arquivo CSV. Ele NÃO se conecta via rede ou SSH.
Para obter métricas válidas da Máquina Virtual (VM) nos experimentos do LCC, este
script DEVE ser executado DENTRO do sistema operacional da VM (via SSH ou serviço local),
e não na máquina física externa que gera a carga com k6.

Nota sobre múltiplos workers:
------------------------------
Ao monitorar um processo via `--target-pid`, as métricas de processo representam
exclusivamente aquele PID. Caso a aplicação web esteja em execução com múltiplos
workers (uvicorn multiprocesso), os demais processos filhos não serão somados ao
PID monitorado. Use `--include-children` para agregar RSS e CPU de toda a árvore
de processos — indispensável com `uvicorn --workers N > 1`, onde o PID principal
é apenas o supervisor e reportaria ~0% de CPU.
"""

import argparse
import csv
import datetime
import os
import signal
import sys
import time

import psutil


class ProcessTreeMonitor:
    """Agrega RSS (MiB) e CPU (%) de um PID e, opcionalmente, de seus descendentes.

    Motivo: com `uvicorn --workers N` (N > 1), o PID principal é apenas o
    supervisor multiprocesso e o consumo real está nos filhos worker. Sem
    agregação da árvore, as métricas de processo reportariam ~0% de CPU e um
    RSS irrelevante, mascarando completamente o perfil computacional.
    """

    def __init__(self, pid: int, include_children: bool = False):
        self.pid = pid
        self.include_children = include_children
        self.root = psutil.Process(pid)
        self.root.cpu_percent(interval=None)
        self._procs = [self.root]
        # PIDs semeados nesta passagem: ler cpu_percent() imediatamente depois
        # calcula um delta sobre ~0 s e o valor explode. São ignorados por uma
        # leitura, devolvendo o valor correto a partir da amostra seguinte.
        self._fresh = set()
        self._rescan()

    def _rescan(self):
        """Revalida a árvore de processos e semeia os contadores de CPU.

        Só os processos NOVOS são semeados. Semear todos a cada refresh faz a
        leitura imediatamente seguinte calcular o delta sobre um intervalo de
        ~0 s; como psutil divide pelo tempo decorrido, o resultado explode
        (medido: proc_cpu_percent = 9106% numa máquina de 8 núcleos) e uma
        única amostra inflada contamina a média do nível inteiro.
        """
        if not self.include_children:
            self._procs = [self.root]
            return
        procs = [self.root]
        try:
            procs.extend(self.root.children(recursive=True))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        prev_ids = {p.pid for p in self._procs}
        self._fresh = set()
        for p in procs:
            if p.pid in prev_ids:
                continue  # já tem contador válido; re-semear causaria o pico
            try:
                p.cpu_percent(interval=None)
                self._fresh.add(p.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self._procs = procs

    def alive(self) -> bool:
        """True se o PID raiz ainda existe e é acessível."""
        return self.root.is_running() and not self.root.status() == psutil.STATUS_ZOMBIE

    def refresh(self) -> None:
        self._rescan()

    def cpu_percent(self) -> float:
        """CPU desde a última chamada, somada na árvore (% de um núcleo).

        Levanta psutil.NoSuchProcess se NENHUM processo da árvore pôde ser lido.
        Devolver 0.0 nesse caso mascararia a morte do servidor como "carga zero",
        gerando gráficos de CPU planos e falsos.
        """
        total = 0.0
        read = 0
        alive = 0
        for p in self._procs:
            try:
                if p.is_running():
                    alive += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if p.pid in self._fresh:
                continue  # semeado agora: delta ~0 produziria um valor absurdo
            try:
                total += p.cpu_percent(interval=None)
                read += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        self._fresh = set()
        if alive == 0:
            # Nenhum processo da árvore está vivo: o servidor morreu.
            raise psutil.NoSuchProcess(self.pid)
        if read == 0:
            # Todos os processos foram semeados nesta passagem (workers novos).
            # A leitura correta vem na amostra seguinte.
            return 0.0
        return total

    def rss_mib(self) -> float:
        """RSS somado da árvore em MiB.

        Levanta psutil.NoSuchProcess se NENHUM processo da árvore pôde ser lido.
        Um RSS de 0.0 é sempre falso para um servidor uvicorn vivo — reportá-lo
        produz curvas de memória planas e mascara a falha de instrumentação.
        """
        total = 0
        read = 0
        for p in self._procs:
            try:
                total += p.memory_info().rss
                read += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        if read == 0:
            raise psutil.NoSuchProcess(self.pid)
        return total / (1024 ** 2)

    def io_counters(self):
        """Soma I/O cumulativa (read_bytes, write_bytes) da árvore; None se indisponível."""
        total_r = total_w = 0
        ok = False
        for p in self._procs:
            try:
                io = p.io_counters()
            except Exception:
                continue
            total_r += getattr(io, "read_bytes", 0) or 0
            total_w += getattr(io, "write_bytes", 0) or 0
            ok = True
        return (total_r, total_w) if ok else None


def parse_args():
    p = argparse.ArgumentParser(
        description="Monitor contínuo de métricas do sistema (CPU, RAM, Swap) com agendamento monotônico."
    )
    p.add_argument(
        "--label",
        default="run",
        help="Identificador do teste ou cenário (ex.: vm_c1_1vcpu_1gb_8vus).",
    )
    p.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="Duração máxima da coleta em segundos (0 = até sinal de interrupção).",
    )
    p.add_argument(
        "--interval",
        type=float,
        default=1.0,
        help="Intervalo de amostragem em segundos (deve ser > 0).",
    )
    p.add_argument(
        "--out-dir",
        default="data/results",
        help="Diretório de saída para gravação incremental do CSV.",
    )
    p.add_argument(
        "--target-pid",
        type=int,
        default=None,
        help="PID específico do processo da aplicação para acompanhar RSS e CPU.",
    )
    p.add_argument(
        "--include-children",
        action="store_true",
        help=(
            "Soma RSS e CPU de todos os descendentes do --target-pid. "
            "Indispensável quando a aplicação usa múltiplos workers "
            "(uvicorn --workers N), pois o PID principal é só o supervisor."
        ),
    )
    p.add_argument(
        "--machine",
        default="local",
        choices=["local", "vm"],
        help="Etiqueta identificadora da origem física/virtual dos dados (local ou vm).",
    )
    args = p.parse_args()

    # Validação estrita de parâmetros de amostragem
    if args.interval <= 0:
        p.error("O argumento --interval deve ser estritamente maior que zero.")
    if args.duration < 0:
        p.error("O argumento --duration não pode ser negativo.")

    return args


def build_fieldnames(num_cores: int, has_pid: bool) -> list:
    fields = [
        "timestamp_utc",
        "elapsed_s",
        "cpu_percent_total",
    ]
    for i in range(num_cores):
        fields.append(f"cpu_core_{i}_percent")
    fields += [
        "mem_total_mib",
        "mem_used_mib",
        "mem_available_mib",
        "mem_percent",
        "swap_total_mib",
        "swap_used_mib",
        "swap_percent",
        "swap_sin_rate_kib_s",
        "swap_sout_rate_kib_s",
        "disk_read_mib_s",
        "disk_write_mib_s",
        "disk_read_iops",
        "disk_write_iops",
        "disk_busy_percent",
    ]
    if has_pid:
        fields += [
            "proc_rss_mib",
            "proc_cpu_percent",
            "proc_read_kib_s",
            "proc_write_kib_s",
        ]
    return fields


def main():
    args = parse_args()
    os.makedirs(args.out_dir, exist_ok=True)

    now_utc = datetime.datetime.now(datetime.timezone.utc)
    ts_str = now_utc.strftime("%Y%m%dT%H%M%SZ")
    filename = os.path.join(
        args.out_dir, f"sys_{args.machine}_{args.label}_{ts_str}.csv"
    )

    proc = None
    if args.target_pid is not None:
        try:
            proc = ProcessTreeMonitor(args.target_pid, args.include_children)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            print(f"[monitor:aviso] PID {args.target_pid} não acessível ou inexistente.")
            proc = None

    num_cores = psutil.cpu_count(logical=True) or 1
    fieldnames = build_fieldnames(num_cores, args.target_pid is not None)

    stop_requested = False

    def _signal_handler(sig, frame):
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    print(f"[monitor] Iniciando coleta de recursos:")
    print(f"          Destino: {filename}")
    print(f"          Intervalo: {args.interval}s | Origem declarada: {args.machine}")
    if args.duration > 0:
        print(f"          Duração máxima programada: {args.duration}s")
    else:
        print("          Duração: até interrupção (Ctrl+C / SIGINT)")
    if proc:
        scope = "árvore de processos (PID + filhos)" if args.include_children else "processo único"
        print(f"          Processo monitorado: PID {proc.pid} [{scope}]")

    # Inicialização dos contadores de CPU do psutil
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(percpu=True, interval=None)

    # Inicialização dos contadores de Swap cumulativos do kernel
    sw_init = psutil.swap_memory()
    prev_sin = getattr(sw_init, "sin", 0)
    prev_sout = getattr(sw_init, "sout", 0)

    # Inicialização dos contadores cumulativos de I/O de disco
    try:
        dio_init = psutil.disk_io_counters()
    except Exception:
        dio_init = None
    prev_dio = dio_init

    # Inicialização dos contadores cumulativos de I/O do processo alvo
    prev_proc_read = 0
    prev_proc_write = 0
    proc_io_supported = True
    if proc is not None:
        io_totals = proc.io_counters()
        if io_totals is None:
            proc_io_supported = False
        else:
            prev_proc_read, prev_proc_write = io_totals

    t_start = time.perf_counter()
    prev_sample_time = t_start
    sample_index = 0

    with open(filename, "w", newline="", buffering=1024) as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        f.flush()

        while not stop_requested:
            # Agendamento monotônico preciso: calcula o instante esperado para a próxima amostra
            sample_index += 1
            target_time = t_start + (sample_index * args.interval)
            now_perf = time.perf_counter()

            sleep_duration = target_time - now_perf
            if sleep_duration > 0:
                time.sleep(sleep_duration)

            current_time = time.perf_counter()
            elapsed = current_time - t_start
            delta_t = max(0.0001, current_time - prev_sample_time)
            prev_sample_time = current_time

            # Interrupção se atingir a duração máxima configurada
            if args.duration > 0 and elapsed >= args.duration:
                break

            # Coleta de métricas de CPU e Memória
            cpu_total = psutil.cpu_percent(interval=None)
            cpu_cores = psutil.cpu_percent(percpu=True, interval=None)
            vm = psutil.virtual_memory()
            sw = psutil.swap_memory()

            # Cálculo diferencial das taxas de swap (KiB/s)
            curr_sin = getattr(sw, "sin", 0)
            curr_sout = getattr(sw, "sout", 0)
            swap_sin_rate = max(0.0, (curr_sin - prev_sin) / 1024.0 / delta_t)
            swap_sout_rate = max(0.0, (curr_sout - prev_sout) / 1024.0 / delta_t)
            prev_sin = curr_sin
            prev_sout = curr_sout

            # Cálculo diferencial das taxas de I/O de disco
            try:
                dio = psutil.disk_io_counters()
            except Exception:
                dio = None
            if dio is not None and prev_dio is not None:
                d_read = max(0, dio.read_bytes - prev_dio.read_bytes)
                d_write = max(0, dio.write_bytes - prev_dio.write_bytes)
                d_rcnt = max(0, dio.read_count - prev_dio.read_count)
                d_wcnt = max(0, dio.write_count - prev_dio.write_count)
                prev_busy = getattr(prev_dio, "busy_time", None)
                curr_busy = getattr(dio, "busy_time", None)
                if prev_busy is not None and curr_busy is not None:
                    disk_busy = max(0.0, (curr_busy - prev_busy) / (delta_t * 1000.0) * 100.0)
                else:
                    disk_busy = 0.0
                disk_read_mib_s = round(d_read / (1024 ** 2) / delta_t, 3)
                disk_write_mib_s = round(d_write / (1024 ** 2) / delta_t, 3)
                disk_read_iops = round(d_rcnt / delta_t, 2)
                disk_write_iops = round(d_wcnt / delta_t, 2)
            else:
                disk_busy = 0.0
                disk_read_mib_s = disk_write_mib_s = 0.0
                disk_read_iops = disk_write_iops = 0.0
            prev_dio = dio

            row = {
                "timestamp_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "elapsed_s": round(elapsed, 3),
                "cpu_percent_total": cpu_total,
            }
            for i in range(num_cores):
                row[f"cpu_core_{i}_percent"] = cpu_cores[i] if i < len(cpu_cores) else 0.0

            row.update({
                "mem_total_mib": round(vm.total / (1024 ** 2), 2),
                "mem_used_mib": round(vm.used / (1024 ** 2), 2),
                "mem_available_mib": round(vm.available / (1024 ** 2), 2),
                "mem_percent": vm.percent,
                "swap_total_mib": round(sw.total / (1024 ** 2), 2),
                "swap_used_mib": round(sw.used / (1024 ** 2), 2),
                "swap_percent": sw.percent,
                "swap_sin_rate_kib_s": round(swap_sin_rate, 2),
                "swap_sout_rate_kib_s": round(swap_sout_rate, 2),
                "disk_read_mib_s": disk_read_mib_s,
                "disk_write_mib_s": disk_write_mib_s,
                "disk_read_iops": disk_read_iops,
                "disk_write_iops": disk_write_iops,
                "disk_busy_percent": round(disk_busy, 2),
            })

            # Métricas do processo alvo (se ativo)
            if args.target_pid is not None:
                if proc is not None:
                    # Revalida a árvore periodicamente (workers podem ser reiniciados)
                    if sample_index % 5 == 0:
                        proc.refresh()
                    try:
                        row["proc_rss_mib"] = round(proc.rss_mib(), 2)
                        row["proc_cpu_percent"] = round(proc.cpu_percent(), 2)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        row["proc_rss_mib"] = ""
                        row["proc_cpu_percent"] = ""
                        proc = None

                    io_totals = proc.io_counters() if (proc is not None and proc_io_supported) else None
                    if io_totals is not None:
                        curr_read, curr_write = io_totals
                        row["proc_read_kib_s"] = round(
                            max(0, curr_read - prev_proc_read) / 1024.0 / delta_t, 2
                        )
                        row["proc_write_kib_s"] = round(
                            max(0, curr_write - prev_proc_write) / 1024.0 / delta_t, 2
                        )
                        prev_proc_read = curr_read
                        prev_proc_write = curr_write
                    else:
                        row["proc_read_kib_s"] = ""
                        row["proc_write_kib_s"] = ""
                else:
                    row["proc_rss_mib"] = ""
                    row["proc_cpu_percent"] = ""
                    row["proc_read_kib_s"] = ""
                    row["proc_write_kib_s"] = ""

            writer.writerow(row)
            f.flush()

            if sample_index % 10 == 0:
                print(
                    f"  [monitor] t={elapsed:.1f}s | CPU={cpu_total:5.1f}% | "
                    f"RAM={row['mem_percent']:5.1f}% | Swap={row['swap_percent']:5.1f}% | "
                    f"Amostras={sample_index}",
                    flush=True,
                )

    print(f"[monitor] Coleta concluída com sucesso. {sample_index} amostras salvas em {filename}")


if __name__ == "__main__":
    main()
