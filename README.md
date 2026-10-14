# Demanda, Provisionamento e Desempenho

Atividade 2 — Grupo: Rafael Barreto da Silva (123110738) & Igor Rafael Menezes de Melo (123110549)

Três aplicações web (apenas backend, Python/FastAPI) com perfis computacionais distintos, usadas para estudar a relação entre **demanda, desempenho, utilização de recursos e provisionamento** (local e em VM VirtualBox).

| Pasta | Perfil | Endpoint de carga | Recurso crítico |
|---|---|---|---|
| [`src/cpu-bound`](src/cpu-bound/) | CPU-bound | `GET /process-image` | CPU |
| [`src/memory-bound`](src/memory-bound/) | Memory-bound | `GET /workload` | RAM (capacidade) |
| [`src/io-bound`](src/io-bound/) | IO-bound | `GET /process` | Disco |

## Estrutura

```
.
├── README.md
├── docs/
│   ├── atividade.md                                   # enunciado da atividade
│   └── virtualbox-tutorial.pdf                        # tutorial de uso da VM
└── src/
    ├── cpu-bound/      ┐
    ├── memory-bound/   ├ cada projeto tem: app/ k6/ metrics/collectors/ plots/
    └── io-bound/       ┘ docs/ (deploy_vm.sh, run_vm_experiments.sh) data/
```

Cada projeto tem seu próprio README com instruções detalhadas de execução local, geração de carga (k6), coleta de métricas e experimentos na VM.

## Execução local (resumo)

```bash
cd src/cpu-bound          # ou src/memory-bound / src/io-bound
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
./run_local_sweep.sh      # sobe o servidor, coleta métricas, roda o k6 e gera os gráficos
```

Requisitos: Python 3.12, [k6](https://k6.io/) instalado no host.

## Experimentos na VM (VirtualBox)

1. Dentro da VM (via SSH): `bash docs/deploy_vm.sh` (dentro da pasta do projeto escolhido).
2. Fora da VM (host/LCC): `VM_IP=<ip> SCENARIO=C1_1vcpu_1gib bash docs/run_vm_experiments.sh`.
3. Repetir para cada cenário de provisionamento (alterando vCPU/RAM no VirtualBox).

A carga é gerada de fora da VM (k6) e as métricas de infraestrutura são coletadas dentro dela, via SSH.

## Resultados

- Resultados brutos: `*/data/results/` (CSV). Gráficos: `*/plots/output/`.
- Resultados da VM devem ficar em `*/data/results/` com prefixo `sys_vm_*`.
- Relatório: 