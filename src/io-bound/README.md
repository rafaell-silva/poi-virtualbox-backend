# POI — Demanda, Provisionamento e Desempenho (IO-bound)

Projeto acadêmico experimental para investigar a relação entre demanda de tráfego,
consumo de recursos e provisionamento (local e em máquina virtual, VirtualBox) em
uma aplicação web com perfil **IO-bound** (limitada por disco).

Esta versão executa **localmente** ou na **VM VirtualBox**, sem dependência de
nuvem, Kubernetes ou EKS.

---

## 1. Perfil Computacional

A aplicação é IO-bound por construção (`app/main.py`, endpoint `GET|POST /process`):

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
├── tests/                       # Testes de fumaça da API (pytest)
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

**No Linux / VM (Bash):**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**No Windows (Git Bash):**
```bash
python -m venv .venv
source .venv/Scripts/activate
pip install -r requirements.txt
```

**No Windows (PowerShell):**
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 3.2. Iniciar a aplicação

- **Linux / Git Bash:** `IO_DIR=/tmp/io_test uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4`
- **PowerShell:** `$env:IO_DIR="C:\temp\io_test"; uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4`
> Para experimentos reais, aponte `IO_DIR` para um volume de disco (não tmpfs).
> Ajuste `--workers` ao número de núcleos; os scripts de varredura usam
> `WORKERS=$(nproc)` por padrão.

### 3.3. Teste rápido
```bash
curl http://127.0.0.1:8000/info
curl "http://127.0.0.1:8000/process?size_mb=8&iterations=1"
```

### 3.4. Varredura local completa
```bash
./run_local_sweep.sh
# personalizando (multi-worker + mix de GET/POST):
SIZE_MIB=8 ITERATIONS=1 VUS_LEVELS="1 2 4 8" DURATION=20 IO_DIR=/data/io \
  WORKERS=8 REQUEST_MIX=mixed SCENARIO=LOCAL REP=1 ./run_local_sweep.sh
```

Cada nível recebe um `run_id`
(`<perfil>_<cenário>_w<workers>_<mix>_<vus>vus_r<rep>_<UTC>`), gravado no CSV do
experimento e no CSV do monitor para o join exato usado nos gráficos.

### 3.5. Execução via Docker / Docker Compose

Você também pode subir o container isolado usando Docker:

**Com Docker Compose:**
```bash
docker compose up --build
```

**Com Docker puro:**
```bash
docker build -t poi-io-bound app/
docker run --rm -p 8000:8000 --cpus="1.0" --memory="512m" -e IO_DIR=/tmp poi-io-bound
```

---

## 4. Geração de Carga com k6

```bash
k6 run k6/load_ramp.js   # modelo fechado
k6 run k6/load_rate.js   # modelo aberto (ramping-arrival-rate)
k6 run k6/load_ramp.js -e TARGET_URL=http://192.168.56.10:8000 -e SIZE_MIB=8
```

Parâmetros: `TARGET_URL`, `SIZE_MIB`, `ITERATIONS`, `OUT_DIR` (e `MAX_RATE` no
`load_rate.js`), além de `REQUEST_MIX`, `RUN_ID`, `SCENARIO`, `VCPUS`, `RAM_GIB`,
`WORKERS` e `REP`. `REQUEST_MIX=mixed` alterna GET/POST; `uniform` mantém só GET.
O k6 grava `data/results/k6_*.csv` com latência (HTTP e fases internas: escrita,
fsync, leitura), RPS, erros e os metadados de execução.

---

## 5. Coleta de Métricas (`system_monitor.py`)

Além de CPU/RAM/Swap, o monitor coleta **I/O de disco**, essencial para o perfil:

- `run_id`, `profile`, `scenario`, `vcpus`, `ram_gib`, `workers`, `request_mix`, `rep`
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

O gerador produz **duas figuras agrupadas** em `plots/output/`, com eixos fixos
no máximo global de cada métrica:

1. `overview_demand.png` — Demanda × latência, vazão, CPU, memória, **I/O de
   disco** e duração de escrita/fsync do app.
2. `overview_time.png` — CPU/memória do sistema e CPU/RSS do processo ao longo
   do tempo.

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

O script deriva `VCPUS`/`RAM_GIB` do nome do cenário e usa `WORKERS=VCPUS` por
padrão (um worker por vCPU); use `WORKERS=1` para a linha de base.

---

## 8. Validação dos Resultados

```bash
python3 tools/validate_results.py src/io-bound/data/results
```

Verifica `run_id` presente e único em cada experimento novo e a existência de
exatamente um `sys_*.csv` com o mesmo `run_id`. CSVs antigos sem `run_id` são
tratados como *legacy* (apenas aviso).

---

> Para a matriz **workers × request_mix × repetições** e os filtros de
> gráficos, veja [`docs/metodologia/README.md`](../../docs/metodologia/README.md).

## 9. Nuvem (OpenStack/LSD)

O roteiro manual completo está em
[`docs/cloud/README.md`](../../docs/cloud/README.md). Na arquitetura com as três
apps na mesma VM, esta aplicação escuta em `127.0.0.1:8002` e é publicada em
`http://<LB>/io/` pelo nginx.

```bash
WORKERS=$(nproc) HOST=127.0.0.1 APP_PORT=8002 \
APP_DIR=/opt/poi/io SERVICE_NAME=poi-io \
IO_DIR=/mnt/poi-data/io \
LOG_DIR=/mnt/poi-data/logs DATA_MOUNT=/mnt/poi-data \
bash docs/deploy_vm.sh
```

O `IO_DIR` aponta para o volume OpenStack (disco real), não para tmpfs. O
`LOG_DIR` mantém os logs no volume.
