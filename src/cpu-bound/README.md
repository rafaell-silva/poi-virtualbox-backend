# POI — Demanda, Provisionamento e Desempenho (CPU-bound)

Projeto acadêmico experimental para investigar a relação entre demanda de tráfego,
consumo de recursos computacionais e provisionamento (local e em máquina virtual,
VirtualBox) em uma aplicação web com perfil **CPU-bound**.

Esta versão executa **localmente** (máquina física ou VM VirtualBox) — não depende
de nuvem, EKS, Kubernetes, HPA ou LoadBalancer.

---

## 1. Perfil Computacional

A aplicação é CPU-bound por construção (`app/main.py`):

- O endpoint `GET /process-image?intensity=N&chunk_kib=K` executa `N` iterações de
  hash **SHA-256** sobre um bloco **determinístico** de `K` KiB.
- Cada iteração hasheia um bloco **≥ 2 KiB**. Isso é essencial: o CPython só libera
  o **GIL** no `hashlib` acima de ~2 KiB. Com blocos grandes, o *threadpool* do
  FastAPI calcula em **vários núcleos em paralelo** com um único worker. Medido com
  `intensity=1000` / `chunk_kib=64`: **~0,7 núcleo com 1 VU → ~3,4 núcleos com 16 VUs**
  (`proc_cpu_percent`), ou seja, ~192 ms de latência a 4 req/s em 1 VU, subindo a
  ~2 500 ms enquanto o RPS trava em ~12–13 req/s. Blocos pequenos (~1 KiB) segurariam
  o GIL e a CPU ficaria presa a 1 núcleo.
  > O teto ~3,4 núcleos (e não 8) é o custo residual do GIL: a criação do objeto
  > `hashlib`, o `update()` de 32 B e o `digest()` ainda rodam sob o GIL. A saturação
  > plena de 8 núcleos exige `--workers N` (processos), não mais threads.
- Não há I/O de disco nem rede de dados por requisição: o tempo é gasto em CPU.
- O trabalho por requisição cresce **linearmente** com `intensity` (e com
  `chunk_kib`), permitindo saturar a CPU e observar o "joelho" de desempenho
  (latência subindo e throughput estagnando).

O bloco é cacheado por tamanho (`_CHUNK_CACHE`), sem gerar entropia nem I/O por
requisição, isolando o perfil puramente computacional.

---

## 2. Estrutura do Repositório

```
cpu-bound/
├── app/
│   ├── main.py                  # Aplicação FastAPI CPU-bound
│   ├── Dockerfile
│   └── requirements.txt
├── k6/
│   ├── load-test.js             # Carga — modelo fechado (rampa de VUs) + CSV
│   └── smoke-test.js            # Validação rápida (30s)
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
├── tests/                       # Testes de fumaça da API (pytest)
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

- **Linux / Git Bash:** `uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4`
- **PowerShell:** `uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 4`

> Ajuste `--workers` ao número de núcleos da máquina. Os scripts de varredura já
> usam `WORKERS=$(nproc)` por padrão (um worker por núcleo); use `WORKERS=1` apenas
> para uma linha de base explícita de um único processo.

### 3.3. Teste rápido

```bash
curl http://127.0.0.1:8000/info
curl "http://127.0.0.1:8000/process-image?intensity=1000&chunk_kib=64"
```

### 3.4. Varredura local completa (servidor + métricas + gráficos)

```bash
./run_local_sweep.sh
# personalizando (multi-worker + mix de GET/POST):
INTENSITY=2000 CHUNK_KIB=64 VUS_LEVELS="1 2 4 8" DURATION=20 \
  WORKERS=8 REQUEST_MIX=mixed SCENARIO=LOCAL REP=1 ./run_local_sweep.sh
```

Cada nível recebe um `run_id` único no formato
`<perfil>_<cenário>_w<workers>_<mix>_<vus>vus_r<rep>_<UTC>`, gravado tanto no
CSV do experimento quanto no CSV do monitor — é o elo de join usado pelos
gráficos e pelo validador.

### 3.5. Execução via Docker / Docker Compose

Você também pode subir o container isolado usando Docker:

**Com Docker Compose:**
```bash
docker compose up --build
```

**Com Docker puro:**
```bash
docker build -t poi-cpu-bound app/
docker run --rm -p 8000:8000 --cpus="0.5" --memory="256m" poi-cpu-bound
```

O script sobe o uvicorn, inicia o `system_monitor.py` e executa cada nível de
demanda via `run_experiment.py` — **ambos recebem o PID do servidor** (`--target-pid`,
com `--include-children`), antes de gerar os gráficos.

> `INTENSITY` controla a carga de CPU por requisição (nº de iterações de hash).
> `ITERATIONS` é um atalho retrocompatível: `INTENSITY = ITERATIONS × 1000`.
> Para saturar a CPU, aumente `INTENSITY` e/ou `WORKERS` (nº de processos uvicorn).

> **Por que `--target-pid` não é opcional.** Sem ele, o CSV de cada nível de
> demanda não grava `proc_rss_mib`/`proc_cpu_percent` e o `generate_plots.py`
> recua para `cpu_percent_total`/`mem_percent` do sistema inteiro. Numa máquina
> de 8 núcleos, 1 worker saturando 1 núcleo aparece como 12,5% — e a curva fica
> indistinguível do ruído de fundo. O mesmo vale para a RAM: a variação de
> dezenas de MiB de um processo some numa máquina de 32 GiB.

---

## 4. Geração de Carga com k6

O teste é um **modelo fechado** (rampa de VUs): cada VU espera a resposta anterior.
Se a CPU saturar, o RPS para de crescer (Lei de Little).

```bash
k6 run k6/load-test.js
k6 run k6/load-test.js -e BASE_URL=http://192.168.56.10:8000 -e INTENSITY=4000
```

Parâmetros: `BASE_URL`, `INTENSITY`, `CHUNK_KIB`, `OUT_DIR`, `VUS`, `DURATION`,
`REQUEST_MIX`, `RUN_ID`, `SCENARIO`, `VCPUS`, `RAM_GIB`, `WORKERS`, `REP`. Ao final,
o k6 grava `data/results/k6_cpu_*.csv`, consumido por `generate_plots.py`.

`REQUEST_MIX=uniform` (padrão) envia apenas `GET`; `REQUEST_MIX=mixed` alterna
`GET`/`POST` no mesmo endpoint, exercitando os dois caminhos da API. O resumo
inclui o `run_id` e os metadados de cenário para o join com o monitor.

---

## 5. Coleta de Métricas (`system_monitor.py`)

Coleta contínua com agendamento monotônico (`perf_counter`), sem acúmulo de atraso.
Métricas registradas:

- `run_id`, `profile`, `scenario`, `vcpus`, `ram_gib`, `workers`, `request_mix`, `rep`
- `elapsed_s`, `timestamp_utc`
- `cpu_percent_total`, `cpu_core_X_percent`
- `mem_total_mib`, `mem_used_mib`, `mem_available_mib`, `mem_percent`
- `swap_total_mib`, `swap_used_mib`, `swap_percent`,
  `swap_sin_rate_kib_s`, `swap_sout_rate_kib_s`
- `disk_read_mib_s`, `disk_write_mib_s`, `disk_read_iops`, `disk_write_iops`,
  `disk_busy_percent`
- Com `--target-pid`: `proc_rss_mib`, `proc_cpu_percent`,
  `proc_read_kib_s`, `proc_write_kib_s`

Uso:

```bash
.venv/bin/python metrics/collectors/system_monitor.py \
    --label cpu_test --target-pid <PID> --include-children --interval 1.0 --machine local
```

> **CPU em máquinas multi-core:** `cpu_percent_total` é a **média aritmética**
> entre todos os núcleos do sistema. Numa máquina de 8 núcleos, um processo
> saturando 1 núcleo aparece como apenas 12,5% — gráficos construídos sobre ela
> ficam planos. Use `proc_cpu_percent` (requer `--target-pid`), que é % de um
> único núcleo e pode passar de 100%; ou a VM de 1 vCPU, onde o total já reflete
> o único núcleo.

> **`uvicorn --workers N` (N > 1):** o PID principal é apenas o supervisor
> multiprocesso e reporta ~0% de CPU. `--include-children` agrega RSS e CPU de
> toda a árvore de processos.

---

## 6. Gráficos (`plots/generate_plots.py`)

Gera, a partir das medições reais em `data/results/`, apenas duas figuras
agrupadas (menos arquivos, eixos fixos no máximo global de cada métrica):

1. `overview_demand.png` — Demanda × latência (média/p90/p95), vazão, CPU
   (núcleos equivalentes), memória (RSS/%), I/O de disco e eficiência (req/s
   por núcleo).
2. `overview_time.png` — CPU/memória do sistema e CPU/RSS do processo ao longo
   do tempo, com eixos fixos para comparar execuções.

```bash
.venv/bin/python plots/generate_plots.py --results-dir data/results --out-dir plots/output
```

**Unidades e porquê.** CPU é expressa em *núcleos equivalentes* (`% / 100 × nº de
núcleos`), priorizando `proc_cpu_percent`. Sem essa conversão, um worker saturando
1 núcleo de 8 aparece como 12,5% e qualquer curva fica plana. Memória é expressa
como **RSS do processo em MiB**; `mem_percent` do sistema entra apenas como eixo
secundário, porque dezenas/h centenas de MiB de um processo são diluídas na RAM
total da máquina. O eixo de CPU traz uma linha horizontal com a capacidade total,
para enxergar o "joelho" de saturação.

---

## 7. Experimento na VM (VirtualBox)

### Restrição de referência
A VM fornecida tem **1 vCPU e 1 GiB de RAM**. Como a carga é CPU-bound, o
ativo escasso é o tempo de CPU do único núcleo — ideal para demonstrar saturação.

### Cenários de provisionamento propostos
| Cenário | vCPU | RAM | Propósito |
|---|---|---|---|
| **C1 (Referência)** | 1 vCPU | 1 GiB | Linha de base |
| **C2** | 2 vCPUs | 1 GiB | Efeito do paralelismo de CPU |
| **C3** | 2 vCPUs | 2 GiB | Paralelismo com folga de memória |
| **C4** | 4 vCPUs | 4 GiB | Saturação residual do gerador/servidor |

### Procedimento
1. **Na VM (via SSH):** `bash docs/deploy_vm.sh`
2. **Fora da VM** (máquina física), gerar a carga externamente:
   ```bash
   VM_IP=192.168.56.10 SCENARIO=C1_1vcpu_1gib DURATION=60s bash docs/run_vm_experiments.sh
   ```

A carga é gerada **de fora da VM** (k6) e as métricas de infraestrutura são
coletadas **dentro** da VM via SSH, conforme exigido pela atividade.

O script deriva `VCPUS`/`RAM_GIB` do nome do cenário (ex.: `C2_2vcpu_1gib`) e usa
`WORKERS=VCPUS` por padrão — ou seja, um worker uvicorn por vCPU. Para uma linha
de base explícita de processo único, rode com `WORKERS=1`.

---

## 8. Validação dos Resultados

```bash
python3 tools/validate_results.py src/cpu-bound/data/results
```

O validador verifica se cada experimento novo tem `run_id`, se não há `run_id`
duplicado e se existe exatamente um `sys_*.csv` com o mesmo `run_id`. CSVs
antigos sem `run_id` são tratados como *legacy* e apenas avisados.

---

## 9. Nuvem (OpenStack/LSD)

O roteiro manual completo está em
[`docs/cloud/README.md`](../../docs/cloud/README.md). Na arquitetura com as três
apps na mesma VM, esta aplicação escuta em `127.0.0.1:8000` e é publicada em
`http://<LB>/cpu/` pelo nginx.

O `deploy_vm.sh` aceita os parâmetros de nuvem:

```bash
WORKERS=$(nproc) HOST=127.0.0.1 APP_PORT=8000 \
APP_DIR=/opt/poi/cpu SERVICE_NAME=poi-cpu \
LOG_DIR=/mnt/poi-data/logs DATA_MOUNT=/mnt/poi-data \
bash docs/deploy_vm.sh
```

`DATA_MOUNT` adiciona `RequiresMountsFor`; `LOG_DIR` redireciona stdout/stderr
para o volume de 1 GB.
