# Metodologia dos Experimentos

Este documento organiza as **convenções e conveniências** para repetir os
experimentos de demanda × provisionamento de forma comparável. Ele não contém
novos dados: os CSVs e PNGs versionados continuam sendo as medições antigas
(*legacy*, sem `run_id`).

O objetivo é responder, com o mesmo workload:

- o desempenho muda ao sair de **1 worker** para **N workers**?
- o tipo de requisição (**GET** vs **GET/POST**) altera latência/vazão?
- os resultados se mantêm dentro de uma faixa quando o experimento é repetido?
- o provisionamento (vCPU/RAM) e o ambiente (local/VM/nuvem) explicam as
  diferenças observadas?

---

## 1. Fatores experimentais

| Fator | Valores | Como é registrado |
|---|---|---|
| Perfil | `cpu`, `memory`, `io` | `profile` |
| Cenário | `LOCAL`, `C1_1vcpu_1gib`, `C2_2vcpu_1gib`, `cloud_general.small`, ... | `scenario` |
| Workers | `1`, `$(nproc)` / vCPUs da VM | `workers` |
| Mix de requisições | `uniform` (só GET), `mixed` (GET/POST) | `request_mix` |
| Repetição | `1..R` | `rep` |
| Demanda | níveis de VUs (ex.: `1 2 4 8`) | `vus` |
| Workload | `INTENSITY`/`CHUNK_KIB`, `SIZE_MIB`/`DWELL_MS`, `SIZE_MIB`/`ITERATIONS` | colunas do CSV |
| Duração | segundos por nível | `duration_s` |

**Constantes dentro de uma comparação:** mesmo perfil, mesmo workload, mesmos
níveis de VUs, mesma duração e mesma máquina/VM. Se um desses mudar, o
experimento deve ser tratado como outro cenário.

---

## 2. Convenções de `run_id` e metadados

O identificador é gerado pelo orquestrador no formato:

```text
<perfil>_<cenário>_w<workers>_<mix>_<vus>vus_r<rep>_<UTC>
```

Exemplo:

```text
cpu_LOCAL_w8_mixed_4vus_r2_20261030T183000Z
```

Os CSVs de experimento, de sistema e o resumo do k6 carregam:

```text
run_id, profile, scenario, vcpus, ram_gib, workers, request_mix, rep
```

Consequências:

- o `run_id` é o **join exato** entre `exp_*`/`k6_*` e `sys_*`;
- `run_id` deve ser único (timestamp + rep + configuração);
- CSVs sem `run_id` são **legacy**: não entram em comparações novas e são
  ignorados quando um filtro é usado.

---

## 3. Matriz recomendada

### 3.1 Matriz mínima (para o tempo disponível)

Para cada perfil, três configurações, com 1 repetição inicial:

| # | Workers | Mix | Objetivo |
|---|---|---|---|
| 1 | 1 | `uniform` | linha de base de processo único |
| 2 | N | `uniform` | efeito de múltiplos workers |
| 3 | N | `mixed` | efeito de variar GET/POST |

N = `nproc` no host local ou o número de vCPUs da VM. Depois, repita as
configurações escolhidas com `REP=2` e `REP=3` para estimar variabilidade.

### 3.2 Matriz completa

```text
perfil ∈ {cpu, memory, io}
workers ∈ {1, N}
mix     ∈ {uniform, mixed}
rep     ∈ {1, 2, 3}
VUs     = níveis fixos (ex.: 1 2 4 8)
```

Isso gera **12 varreduras por perfil** (2 × 2 × 3) e 36 no total. Cada varredura
contém todos os níveis de VUs.

### 3.3 Planilha de acompanhamento

Use [`matriz.csv`](matriz.csv) para registrar o que já foi executado. Preencha
`status` (`pendente`/`ok`/`falhou`), os `run_ids` e observações. O arquivo é um
**template**: não há resultados novos nele. Para as repetições, duplique as
linhas com `rep=2` e `rep=3`; para VM/cloud, troque `cenario` e `workers` pelos
valores do ambiente.

---

## 4. Como executar manualmente

### 4.1 Varredura local (CPU como exemplo)

```bash
cd src/cpu-bound

# 1) Linha de base: 1 worker, só GET
WORKERS=1 REQUEST_MIX=uniform REP=1 \
VUS_LEVELS="1 2 4 8" DURATION=30 \
./run_local_sweep.sh

# 2) Multi-worker, só GET
WORKERS=$(nproc) REQUEST_MIX=uniform REP=1 \
VUS_LEVELS="1 2 4 8" DURATION=30 \
./run_local_sweep.sh

# 3) Multi-worker, GET/POST
WORKERS=$(nproc) REQUEST_MIX=mixed REP=1 \
VUS_LEVELS="1 2 4 8" DURATION=30 \
./run_local_sweep.sh

# 4) Repetições (repita 2 e 3 trocando REP)
WORKERS=$(nproc) REQUEST_MIX=mixed REP=2 \
VUS_LEVELS="1 2 4 8" DURATION=30 \
./run_local_sweep.sh
```

Para memory-bound, acrescente `SIZE_MIB`/`DWELL_MS`; para IO-bound,
`SIZE_MIB`/`ITERATIONS` e garanta que `IO_DIR` aponta para disco real (na nuvem,
o volume).

### 4.2 VM/cloud

Na VM/cloud, o orquestrador usa `SSH_HOST`/`SSH_PORT` separados de
`TARGET_URL`, além de `SERVICE` e `REMOTE_APP_DIR`:

```bash
TARGET_URL="http://${LB_VIP}:${LB_HTTP_PORT}/cpu" \
SSH_HOST="$LB_VIP" SSH_PORT="$LB_SSH_PORT" \
SERVICE=poi-cpu REMOTE_APP_DIR=/opt/poi/cpu \
REMOTE_RESULTS_DIR=/mnt/poi-data/results \
SCENARIO=cloud_general.small \
VCPUS=<vcpus> RAM_GIB=<ram> WORKERS=<nproc> \
REQUEST_MIX=mixed REP=1 \
./docs/run_vm_experiments.sh
```

Antes de cada perfil, pare as outras aplicações para não disputar CPU/RSS:

```bash
ssh -i "$SSH_KEY" -p "$LB_SSH_PORT" "${VM_USER}@${LB_VIP}" \
  "sudo systemctl stop poi-memory poi-io"
```

### 4.3 Regras de comparabilidade

- Mesmo workload e mesmos `VUS_LEVELS`/`DURATION` entre as células.
- Mesma máquina/VM e mesmo `WORKERS` dentro da célula.
- Ordem fixa ou documentada; se possível, intercale as configurações para
  reduzir deriva térmica/rede.
- Uma execução de aquecimento (descartada) antes da primeira medição.
- Na nuvem, registre se o teste foi via LB ou por túnel SSH direto (use
  `SCENARIO` diferente, ex.: `cloud_lb_...` vs `cloud_direct_...`).
- Não misture CSVs legacy com CSVs novos na mesma comparação.

---

## 5. Como selecionar e comparar resultados

O gerador de gráficos agora aceita filtros pelos metadados. Primeiro, veja o que
existe:

```bash
python plots/generate_plots.py --results-dir data/results --list-runs
```

Exemplo de saída:

```text
run_id                                     scenario  workers  request_mix  rep  vus  arquivo
cpu_LOCAL_w1_uniform_1vus_r1_...           LOCAL     1        uniform      1    1    ...
cpu_LOCAL_w8_mixed_1vus_r1_...             LOCAL     8        mixed        1    1    ...
(legacy)                                   -         -        -            -    1    ...
```

### 5.1 Filtros disponíveis

| Flag | Efeito |
|---|---|
| `--exclude-legacy` | ignora CSVs sem `run_id` |
| `--scenario` | filtra cenário (vírgula para vários) |
| `--workers` | filtra nº de workers |
| `--request-mix` | filtra `uniform`/`mixed` |
| `--rep` | filtra repetição |
| `--run-id` | filtra um `run_id` exato |
| `--list-runs` | lista metadados e não gera gráficos |

Quando qualquer filtro é usado, CSVs legacy são automaticamente ignorados.

### 5.2 Comparar 1 worker × N workers

Gere uma figura por configuração em diretórios separados:

```bash
python plots/generate_plots.py --results-dir data/results \
  --exclude-legacy --workers 1 \
  --out-dir plots/output/w1

python plots/generate_plots.py --results-dir data/results \
  --exclude-legacy --workers 8 \
  --out-dir plots/output/w8
```

Depois compare `overview_demand.png`/`overview_time.png` lado a lado. Os eixos
são fixados no máximo global **da seleção**, então use a mesma escala ao
comparar; se quiser escala idêntica entre workers, gere as duas seleções juntas
sem `--workers` (mas aí as linhas se sobrepõem).

### 5.3 Comparar `uniform` × `mixed`

```bash
python plots/generate_plots.py --exclude-legacy --request-mix uniform \
  --out-dir plots/output/uniform
python plots/generate_plots.py --exclude-legacy --request-mix mixed \
  --out-dir plots/output/mixed
```

### 5.4 Repetições

Hoje cada repetição é um ponto separado. Para comparar:

```bash
python plots/generate_plots.py --exclude-legacy --rep 1 --out-dir plots/output/rep1
python plots/generate_plots.py --exclude-legacy --rep 2 --out-dir plots/output/rep2
python plots/generate_plots.py --exclude-legacy --rep 3 --out-dir plots/output/rep3
```

A agregação automática (média ± desvio/IC) ainda não está implementada; os CSVs
têm `rep` e podem ser agregados em planilha/Python quando os dados existirem.

---

## 6. Validação

Antes de gerar gráficos, valide a integridade:

```bash
python3 tools/validate_results.py src/*/data/results
```

O validador verifica:

- `run_id` presente em experimentos novos;
- `run_id` único;
- exatamente um `sys_*.csv` com o mesmo `run_id`;
- monitores `_all` não exigem experimento;
- legacy é avisado, não quebra.

---

## 7. Checklist antes de coletar dados

- [ ] Workload fixo e documentado (`INTENSITY`/`CHUNK_KIB`, `SIZE_MIB`/`DWELL_MS`, ...).
- [ ] `WORKERS` definido (1 e N).
- [ ] `REQUEST_MIX` definido (`uniform` e `mixed`).
- [ ] `REP` definido (1..R).
- [ ] `VUS_LEVELS` e `DURATION` iguais entre células.
- [ ] Na nuvem: outras apps paradas; `TARGET_URL`/`SCENARIO` indicam LB ou direto.
- [ ] Aquecimento executado e descartado.
- [ ] `run_vm_experiments.sh`/`run_local_sweep.sh` concluído sem erro.
- [ ] `tools/validate_results.py` retorna 0.
- [ ] `--list-runs` mostra os `run_id` esperados.
- [ ] `matriz.csv` atualizado com `status` e `run_ids`.

---

## 8. Estado atual (sem novos dados)

- Nenhuma execução nova foi feita nesta etapa.
- Os CSVs/PNGs versionados são **legacy** (sem `run_id`, medidos com 1 worker).
- Os filtros e a listagem de runs estão implementados e testados, prontos para
  quando houver dados novos.
- A matriz em `matriz.csv` está **pendente**; preencha-a ao executar.
- Enquanto não houver dados novos, use `--exclude-legacy` para deixar claro que
  a figura não está misturando medições antigas com a metodologia nova.
