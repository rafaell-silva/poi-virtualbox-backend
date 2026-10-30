#!/usr/bin/env python3
"""Valida a integridade dos resultados experimentais em data/results/.

Regras verificadas (para execuções novas, com `run_id`):

1. Todo CSV de experimento (`exp_*.csv` / `k6_*.csv`) deve ter `run_id`.
2. `run_id` não pode se repetir entre arquivos de experimento.
3. Todo experimento novo deve ter exatamente um CSV de sistema
   (`sys_*.csv`) com o mesmo `run_id`.
4. Arquivos sem `run_id` são tratados como *legacy*: apenas avisados, nunca
   quebram a validação. Eles ficam fora dos gráficos comparativos novos.

Uso:
    python3 tools/validate_results.py src/cpu-bound/data/results
    python3 tools/validate_results.py src/*/data/results
"""

from __future__ import annotations

import argparse
import csv
import glob
import os
import sys
from collections import defaultdict


def read_metric_csv(path: str) -> dict[str, str]:
    """Lê um CSV no formato `metric,value` e devolve um dicionário."""
    with open(path, newline="", encoding="utf-8") as f:
        return {row["metric"]: row["value"] for row in csv.DictReader(f)}


def read_sys_run_id(path: str) -> str | None:
    """Extrai o `run_id` de um CSV de sistema (coluna ou None)."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        try:
            row = next(reader)
        except StopIteration:
            return None
        value = (row.get("run_id") or "").strip()
        return value or None


def validate_dir(results_dir: str) -> tuple[list[str], list[str]]:
    """Valida um diretório. Retorna (erros, avisos)."""
    errors: list[str] = []
    warnings: list[str] = []

    exp_files = sorted(
        glob.glob(os.path.join(results_dir, "exp_*.csv"))
        + glob.glob(os.path.join(results_dir, "k6_*.csv"))
    )
    sys_files = sorted(glob.glob(os.path.join(results_dir, "sys_*.csv")))

    sys_by_run: dict[str, list[str]] = defaultdict(list)
    for path in sys_files:
        run_id = read_sys_run_id(path)
        if run_id:
            sys_by_run[run_id].append(path)
        else:
            warnings.append(f"[legacy] sys sem run_id: {os.path.basename(path)}")

    seen_runs: dict[str, str] = {}
    legacy_exps = 0
    for path in exp_files:
        name = os.path.basename(path)
        try:
            data = read_metric_csv(path)
        except (KeyError, OSError, csv.Error) as exc:
            errors.append(f"CSV de experimento inválido: {name} ({exc})")
            continue

        run_id = (data.get("run_id") or "").strip()
        if not run_id:
            legacy_exps += 1
            warnings.append(f"[legacy] experimento sem run_id: {name}")
            continue

        if run_id in seen_runs:
            errors.append(
                f"run_id duplicado '{run_id}': {name} e {seen_runs[run_id]}"
            )
        else:
            seen_runs[run_id] = name

        matches = sys_by_run.get(run_id, [])
        if len(matches) == 0:
            errors.append(f"experimento '{name}' sem CSV de sistema (run_id={run_id})")
        elif len(matches) > 1:
            errors.append(
                f"run_id '{run_id}' com {len(matches)} CSVs de sistema: "
                + ", ".join(os.path.basename(p) for p in matches)
            )

    # CSVs de sistema novos que não têm experimento correspondente.
    for run_id, paths in sys_by_run.items():
        if run_id.endswith("_all"):
            continue  # monitor contínuo da varredura inteira
        if run_id not in seen_runs:
            errors.append(
                f"CSV de sistema sem experimento (run_id={run_id}): "
                + ", ".join(os.path.basename(p) for p in paths)
            )

    print(f"[validate] {results_dir}")
    print(
        f"           experimentos={len(exp_files)} "
        f"(novos={len(seen_runs)}, legacy={legacy_exps}) | "
        f"sistemas={len(sys_files)}"
    )
    for warning in warnings:
        print(f"  [aviso] {warning}")
    for error in errors:
        print(f"  [erro]  {error}")
    if not errors:
        print("  OK: nenhum problema encontrado.")
    return errors, warnings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "results_dirs",
        nargs="+",
        help="Diretórios data/results/ a validar (aceita glob do shell).",
    )
    args = parser.parse_args()

    failed = False
    for results_dir in args.results_dirs:
        if not os.path.isdir(results_dir):
            print(f"[validate][erro] diretório inexistente: {results_dir}")
            failed = True
            continue
        errors, _ = validate_dir(results_dir)
        failed = failed or bool(errors)

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
