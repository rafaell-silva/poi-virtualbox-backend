# POI — Demanda, Provisionamento e Desempenho (Memory-bound)

Projeto acadêmico experimental para investigar a relação entre demanda de tráfego, consumo de recursos computacionais e estratégias de provisionamento em aplicações web com perfil **Memory-bound**.

---

## 1. Perfil Computacional e Limitações

### Pressão sobre Capacidade vs. Saturação de Largura de Banda
A carga de trabalho desta aplicação foi implementada com foco em **pressão sobre a capacidade de memória** (*memory capacity pressure*):
- **Pressão sobre Capacidade (Capacity Pressure)**: O endpoint aloca buffers contíguos de bytes (`bytearray`) na heap do processo e realiza *page touching* (escrita em blocos de 64 KiB via `slice assignment` nativo em C no CPython). Isso obriga o kernel Linux a mapear páginas físicas de RAM (evitando *lazy page allocation* e deduplicação de páginas zeradas). A retenção desses dados durante o ciclo de vida da requisição eleva o Resident Set Size (RSS) da aplicação. Em máquinas com pouca RAM física, essa pressão força o kernel a acionar swap/paginação, provocando degradação drástica de latência.
- **Saturação de Largura de Banda (Bandwidth Saturation)**: Saturação contínua dos canais de memória DRAM (dezenas de GB/s) exigiria rotinas em C/Assembly altamente vetorizadas com SIMD/AVX. Em Python/CPython, loops iterativos byte-a-byte introduzem sobrecarga de interpretação de bytecode, tornando o gargalo predominantemente CPU-bound. Nesta aplicação, a manipulação em blocos de 64 KiB reduz ao mínimo o custo do interpretador, mantendo o teste centrado no tempo de alocação, retenção e consumo de RAM, sem fazer falsas suposições de saturação teórica de barramento.

### Unidades de Medida
Todos os cálculos e métricas utilizam unidades binárias padrão IEC:
- $1\text{ KiB} = 1\,024\text{ bytes}$
- $1\text{ MiB} = 1\,024 \times 1\,024\text{ bytes} = 1\,048\,576\text{ bytes}$
- $1\text{ GiB} = 1\,024 \times 1\,024 \times 1\,024\text{ bytes}$

---

## 2. Estrutura do Repositório

```
memory-bound/
├── app/
│   └── main.py                  # Aplicação FastAPI Memory-bound (proteção 503 e snapshots)
├── tests/
│   └── test_app.py              # Bateria de testes funcionais automatizados
├── k6/
│   ├── load_ramp.js             # Gerador de carga — Modelo Fechado (Closed VU Ramp)
│   └── load_rate.js             # Gerador de carga — Modelo Aberto (Open Arrival Rate)
├── metrics/
│   └── collectors/
│       ├── system_monitor.py    # Monitor de SO com agendamento monotônico e taxas de swap
│       └── run_experiment.py    # Executor integrado de experimentos locais
├── data/
│   └── results/                 # CSVs brutos coletados nos testes (dados reais preservados)
├── plots/
│   ├── generate_plots.py        # Gerador de gráficos comparativos
│   └── output/                  # Imagens PNG geradas a partir das medições reais
├── docs/
│   ├── deploy_vm.sh             # Script de deploy e systemd para a VM Ubuntu
│   └── run_vm_experiments.sh    # Orquestrador de carga externa e coleta remota via SSH
├── run_local_sweep.sh           # Varredura coordenada de demanda em ambiente local
└── requirements.txt             # Dependências Python do projeto
```

---

## 3. Segurança e Limites de Concorrência

Para proteger o sistema operacional (especialmente em ambientes com 1 GiB de RAM) contra esgotamento súbito de memória e o *OOM Killer* do Linux:
1. **Limite por Requisição Individual (`MAX_SIZE_MIB`)**: padrão de **256 MiB** (configurável via variável de ambiente).
2. **Limite Agregado Concorrente por Worker (`MAX_CONCURRENT_MIB`)**: padrão de **384 MiB** (configurável).
   - Se a soma dos buffers mantidos por requisições simultâneas em um worker ultrapassar este limite, a API recusa a requisição imediatamente com código HTTP **`503 Service Unavailable`** e cabeçalho `Retry-After: 1`.
   - *Atenção arquitetural*: este limite atua por processo worker. Se forem configurados $N$ workers no uvicorn, o limite global do sistema é multiplicado por $N$. Em máquinas pequenas de 1 GiB de RAM, recomenda-se manter 1 único worker ativo.
3. **Cabeçalhos Anti-Cache**: Todas as respostas de `/workload` retornam `Cache-Control: no-store, no-cache, must-revalidate` para garantir que nenhuma requisição de benchmark seja mascarada por cache intermediário.

---

## 4. Instruções de Instalação e Execução Local

### 4.1. Ambiente Virtual
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```
### 4.2. Iniciar a Aplicação
```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1
```

### 4.3. Execução Local
```bash
./run_local_sweep.sh
# personalizando:
SIZE_MIB=8 DWELL_MS=200 VUS_LEVELS="1 2 4 8 16 32" DURATION=20 ./run_local_sweep.sh
```

O script sobe o uvicorn, inicia o `system_monitor.py` e executa cada nível de
demanda via `run_experiment.py` — **ambos recebem o PID do servidor** (`--target-pid`,
com `--include-children`), antes de gerar os gráficos.

> **`DWELL_MS` é o parâmetro que faz o workload aparecer como memory-bound.**
> Com `dwell_ms=0` o buffer morre em ~6 ms e a janela de residência da memória é
> minúscula frente ao período de amostragem (1 s) — o RSS mal se move. Com
> `dwell_ms=200`, o `time.sleep` libera o GIL e outras requisições conseguem
> alocar enquanto a anterior ainda retém o seu buffer, de modo que o RSS cresce
> monotonicamente com a demanda. Medido localmente (`size_mib=8`, 1 worker):

  | VUs | 1 | 2 | 4 | 8 | 16 | 32 |
  |---|---|---|---|---|---|---|
  | RSS médio do processo (MiB) | 55 | 71 | 103 | 168 | 296 | 554 |
  | CPU média do processo (% de 1 núcleo) | 2 | 4 | 7 | 15 | 27 | 52 |
  | RAM do sistema (%) | 38,5 | 38,6 | 38,6 | 38,9 | 39,3 | 40,2 |

  Note a coluna de RAM do sistema: 500 MiB a mais representam <2% de 32 GiB e
  praticamente não aparecem — é por isso que o RSS do processo é a métrica de
  referência.

> **`SIZE_MIB` é mantido em 8 MiB de propósito.** Com `MAX_CONCURRENT_MIB=384`
> há folga para ~48 buffers simultâneos, então 32 VUs não disparam HTTP 503 e a
> curva de RSS permanece limpa. `size_mib=64` com a mesma cota estoura o limite
> a partir de 8 VUs e gera >48% de erros, o que contamina latência e vazão.

---

## 5. Estratégia de Geração de Carga (k6)

### 5.1. Modelo Fechado (`k6/load_ramp.js`)
- **Conceito**: Usuários Virtuais (VUs) executam requisições sequencialmente. O próximo ciclo aguarda a resposta da requisição anterior e uma pausa de 100 ms (`sleep(0.1)`).
- **Implicação**: A taxa de requisições por segundo (RPS) depende diretamente do tempo de resposta da aplicação. Se a aplicação degradar, o throughput cai naturalmente.
- **Execução**:
  ```bash
  k6 run k6/load_ramp.js -e TARGET_URL=http://127.0.0.1:8000 -e SIZE_MIB=8 -e ITERATIONS=1 -e DWELL_MS=200 -e DWELL_MS=200
  ```

### 5.2. Modelo Aberto (`k6/load_rate.js`)
- **Conceito**: Dispara requisições a uma taxa constante ou crescente de chegadas por segundo (`ramping-arrival-rate`), independentemente de quando as requisições anteriores terminam.
- **Implicação**: Se a aplicação saturar e a latência explodir, as requisições continuam sendo enviadas na mesma taxa. Caso o pool de VUs (`maxVUs`) se esgote, o k6 descarta requisições e contabiliza a métrica **`dropped_iterations`**.
- **Atenção**: Se `dropped_iterations > 0`, a taxa programada NÃO foi integralmente entregue à aplicação.
- **Execução**:
  ```bash
  k6 run k6/load_rate.js -e TARGET_URL=http://127.0.0.1:8000 -e SIZE_MIB=8 -e DWELL_MS=200 -e MAX_RATE=10
  ```

### 5.3. Exportação de Séries Temporais Contínuas
Para gerar um trace detalhado com cada amostra ao longo do tempo (além do sumário em CSV):
```bash
k6 run --out json=data/results/k6_timeline.json k6/load_ramp.js
```

---

## 6. Coleta de Métricas do Sistema (`system_monitor.py`)

O coletor opera com agendamento monotônico (`time.perf_counter()`), garantindo intervalos rigorosamente regulares sem acúmulo de atraso e sem disparo duplo de amostras.

Métricas registradas no CSV:
- `elapsed_s` e `timestamp_utc` (ISO 8601 UTC explícito)
- `cpu_percent_total` e `cpu_core_X_percent`
- `mem_total_mib`, `mem_used_mib`, `mem_available_mib`, `mem_percent`
- `swap_total_mib`, `swap_used_mib`, `swap_percent`
- `swap_sin_rate_kib_s` e `swap_sout_rate_kib_s` (taxas diferenciais entre amostras)
- `proc_rss_mib` e `proc_cpu_percent` (se `--target-pid` for fornecido)

Uso:
```bash
.venv/bin/python metrics/collectors/system_monitor.py \
    --label local_test --target-pid <PID> --include-children --duration 60 --interval 1.0 --machine local
```
> **Nota de rastreabilidade**: A flag `--machine local` ou `--machine vm` serve apenas como rótulo identificador no nome do arquivo. Para coletar métricas da VM, o script deve rodar dentro do sistema operacional da VM.

> **Por que `--target-pid` não é opcional.** Sem ele, o CSV não grava
> `proc_rss_mib`/`proc_cpu_percent` e o `generate_plots.py` recua para
> `mem_percent` do sistema inteiro — 500 MiB a mais são <2% de 32 GiB e a curva
> fica plana. Com `uvicorn --workers N > 1`, o PID principal é só o supervisor e
> reporta ~0% de CPU; use `--include-children` para agregar a árvore.

---

## 7. Geração de Gráficos

O script `plots/generate_plots.py` lê os CSVs de experimentos reais e métricas de sistema salvos em `data/results/` e produz 7 gráficos comparativos em `plots/output/`:
1. `latency_vs_demand.png`: Demanda (VUs) × Latência HTTP (média, p90, p95).
2. `rps_vs_demand.png`: Demanda (VUs) × Vazão em requisições por segundo (RPS).
3. `cpu_over_time.png`: CPU em **núcleos equivalentes** (processo + sistema).
4. `memory_over_time.png`: **RSS do processo (MiB)** como série principal, com a RAM do sistema (%) no eixo secundário.
5. `proc_resources_over_time.png`: CPU% e RSS do processo alvo ao longo do tempo.
6. `disk_io_over_time.png`: Leitura/escrita de disco (MiB/s).
7. `demand_vs_resources.png`: Demanda × CPU (núcleos) / Memória (RSS MiB) / Disco.

Para gerar:
```bash
.venv/bin/python plots/generate_plots.py --results-dir data/results --out-dir plots/output
```

**Unidades e porquê.** CPU é expressa em *núcleos equivalentes* (`% / 100 × nº de
núcleos`), priorizando `proc_cpu_percent`. Sem essa conversão, um worker saturando
1 núcleo de 8 aparece como 12,5% e qualquer curva fica plana. Memória é expressa
como **RSS do processo em MiB**; `mem_percent` do sistema entra apenas como eixo
secundário, porque dezenas/h centenas de MiB de um processo são diluídas na RAM
total da máquina.

---

## 8. Experimentos Futuros no VirtualBox (LCC)

### Restrição do Ambiente do Professor
A máquina virtual fornecida possui **1 vCPU e 1 GiB de RAM**.
- Essa configuração é bastante enxuta: o Ubuntu Server consome cerca de 300–400 MiB de RAM em repouso, restando aproximadamente 600 MiB úteis.
- Parâmetros padrão recomendados para o teste na VM: buffer de **8 MiB**, **1 iteração**, **dwell de 200 ms** e concorrência progressiva (**1, 2, 4, 8, 12 VUs**).

### Cenários de Provisionamento Propostos
Caso o professor autorize alteração dos recursos no VirtualBox:
| Cenário | vCPU | RAM | Propósito Investigativo |
|---|---|---|---|
| **C1 (Referência)** | 1 vCPU | 1 GiB | Linha de base da atividade (restrição do professor) |
| **C2** | 1 vCPU | 2 GiB | Isola o efeito de aumento de memória sob a mesma capacidade de CPU |
| **C3** | 2 vCPUs | 2 GiB | Avalia o ganho de concorrência com paralelismo de CPU |
| **C4** | 2 vCPUs | 4 GiB | Provisionamento ampliado para verificar saturação residual |

> *Caso a configuração de 1 vCPU e 1 GiB seja fixa e inalterável pelo professor*, os experimentos devem se concentrar na variação de demanda sobre esse cenário único de referência (C1), esclarecendo previamente com o docente como cumprir o requisito de provisionamento.

### Procedimento de Execução na VM:
1. Na VM (via SSH): executar `docs/deploy_vm.sh`.
2. Na máquina física / externa (fora da VM):
   ```bash
   VM_IP=192.168.56.10 SCENARIO=C1_1vcpu_1gib SIZE_MIB=8 DWELL_MS=200 bash docs/run_vm_experiments.sh
   ```
   O script dispara a carga via rede externa e aciona a coleta de métricas de infraestrutura dentro da VM via SSH, transferindo os resultados ao final.