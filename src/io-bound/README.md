# POI — Demanda, Provisionamento e Desempenho (IO-bound)

Projeto acadêmico experimental para investigar a relação entre demanda de tráfego,
consumo de recursos e provisionamento (local e em máquina virtual, VirtualBox) em
uma aplicação web com perfil **IO-bound** (limitada por disco).

Esta versão executa **localmente** ou na **VM VirtualBox**, sem dependência de
nuvem, Kubernetes ou EKS.

---

## 1. Perfil Computacional

A aplicação é IO-bound por construção (`app/main.py`, endpoint `GET /process`):

1. Escreve `size_mb` MiB em disco, em blocos de `chunk_kb` KiB (`os.write`).
2. Chama **`os.fsync()`** para forçar a descarga física do *page cache* para o
   dispositivo de bloco. **Esta etapa é o que torna a carga limitada por disco** —
   sem ela, a escrita ficaria em memória e a requisição não seria IO-bound.
3. Lê o arquivo de volta (`os.read`).
4. Remove o arquivo.

Parâmetros: `size_mb`, `iterations`, `chunk_kb`, `fsync`.

### Atenção — diretório de I/O (`IO_DIR`)
Se `IO_DIR` for um sistema de arquivos em RAM (`tmpfs`, `/dev/shm`, ou um `/tmp`
montado em tmpfs), a carga deixa de ser IO-bound. Para os experimentos, aponte
`IO_DIR` para um **volume de disco real** (ex.: `/data/io` na VM, no disco do
VirtualBox).

---

## 2. Estrutura do Repositório

```
io-bound/
├── app/
│   ├── main.py                  # Aplicação FastAPI IO-bound (write+fsync+read)
│   ├── Dockerfile
│   └── requirements.txt
├── k6/
│   ├── load_ramp.js             # Carga — modelo fechado (rampa de VUs) + CSV
│   └── load_rate.js             # Carga — modelo aberto (taxa de chegada) + CSV
├── metrics/
│   └── collectors/
│       ├── system_monitor.py    # Monitor de SO (CPU/RAM/Swap/Disco + processo)
│       └── run_experiment.py    # Executor integrado de experimentos locais
├── data/results/                # CSVs brutos coletados nos testes
├── plots/
│   ├── generate_plots.py        # Gerador de gráficos comparativos
│   └── output/                  # PNGs gerados a partir das medições reais
├── docs/
│   ├── deploy_vm.sh             # Deploy + systemd na VM Ubuntu
│   └── run_vm_experiments.sh    # Carga externa (k6) + coleta interna via SSH
├── run_local_sweep.sh           # Varredura local de níveis de demanda
└── requirements.txt
```

---

## 3. Instalação e Execução Local

### 3.1. Ambiente virtual
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 3.2. Iniciar a aplicação
```bash
IO_DIR=/tmp/io_test .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```
> Para experimentos reais, substitua `IO_DIR` por um volume de disco (não tmpfs).

### 3.3. Teste rápido
```bash
curl http://127.0.0.1:8000/info
curl "http://127.0.0.1:8000/process?size_mb=8&iterations=1"
```

### 3.4. Varredura local completa
```bash
./run_local_sweep.sh
# personalizando:
SIZE_MIB=8 ITERATIONS=1 VUS_LEVELS="1 2 4 8" DURATION=20 IO_DIR=/data/io ./run_local_sweep.sh
```

---

## 4. Geração de Carga com k6

```bash
k6 run k6/load_ramp.js   # modelo fechado
k6 run k6/load_rate.js   # modelo aberto (ramping-arrival-rate)
k6 run k6/load_ramp.js -e TARGET_URL=http://192.168.56.10:8000 -e SIZE_MIB=8
```

Parâmetros: `TARGET_URL`, `SIZE_MIB`, `ITERATIONS`, `OUT_DIR` (e `MAX_RATE` no
`load_rate.js`). O k6 grava `data/results/k6_*.csv` com latência (HTTP e fases
internas: escrita, fsync, leitura), RPS e erros.

---

## 5. Coleta de Métricas (`system_monitor.py`)

Além de CPU/RAM/Swap, o monitor coleta **I/O de disco**, essencial para o perfil:

- `disk_read_mib_s`, `disk_write_mib_s` — taxas de leitura/escrita
- `disk_read_iops`, `disk_write_iops` — operações por segundo
- `disk_busy_percent` — percentual de tempo com o dispositivo ocupado
- Com `--target-pid`: `proc_read_kib_s`, `proc_write_kib_s` (I/O do processo)

```bash
.venv/bin/python metrics/collectors/system_monitor.py \
    --label io_test --target-pid <PID> --interval 1.0 --machine local
```

---

## 6. Gráficos (`plots/generate_plots.py`)

1. `latency_vs_demand.png` — Demanda × Latência (avg/p90/p95)
2. `rps_vs_demand.png` — Demanda × Vazão (RPS)
3. `cpu_over_time.png` — CPU (%) ao longo do tempo
4. `memory_over_time.png` — Memória (%) ao longo do tempo
5. `proc_resources_over_time.png` — CPU% e RSS do processo alvo
6. `disk_io_over_time.png` — **Leitura/escrita de disco (MiB/s)**
7. `demand_vs_resources.png` — Demanda × CPU / Memória / **Disco**

```bash
.venv/bin/python plots/generate_plots.py --results-dir data/results --out-dir plots/output
```

---

## 7. Experimento na VM (VirtualBox)

### Cenários de provisionamento propostos
| Cenário | vCPU | RAM | Disco | Propósito |
|---|---|---|---|---|
| **C1 (Referência)** | 1 vCPU | 1 GiB | padrão | Linha de base |
| **C2** | 1 vCPU | 1 GiB | disco mais rápido (SSD) | Efeito da velocidade de disco |
| **C3** | 2 vCPUs | 1 GiB | padrão | Paralelismo de CPU |
| **C4** | 2 vCPUs | 2 GiB | SSD | Provisionamento ampliado |

### Procedimento
1. **Na VM (via SSH):** crie o volume e o serviço:
   ```bash
   sudo mkdir -p /data/io && sudo chown "$USER" /data/io
   IO_DIR=/data/io bash docs/deploy_vm.sh
   ```
2. **Fora da VM** (máquina física), gerar a carga externamente:
   ```bash
   VM_IP=192.168.56.10 SCENARIO=C1_1vcpu_1gib DURATION=60s bash docs/run_vm_experiments.sh
   ```

A carga é gerada **de fora da VM** (k6) e as métricas de infraestrutura são
coletadas **dentro** da VM via SSH.
