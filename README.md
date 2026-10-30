# Demanda, Provisionamento e Desempenho

Atividade 2 — Grupo: Rafael Barreto da Silva (123110738) & Igor Rafael Menezes de Melo (123110549)

Três aplicações web (apenas backend, Python/FastAPI) com perfis computacionais distintos, usadas para estudar a relação entre **demanda, desempenho, utilização de recursos e provisionamento** (local e em VM VirtualBox).

| Pasta | Perfil | Endpoint de carga | Recurso crítico |
|---|---|---|---|
| [`src/cpu-bound`](src/cpu-bound/) | CPU-bound | `GET\|POST /process-image` | CPU |
| [`src/memory-bound`](src/memory-bound/) | Memory-bound | `GET\|POST /workload` | RAM (capacidade) |
| [`src/io-bound`](src/io-bound/) | IO-bound | `GET\|POST /process` | Disco |

## Estrutura

```
.
├── README.md
├── requirements-dev.txt                              # pytest/httpx (testes)
├── pytest.ini
├── tests/                                            # testes do join/validador
├── tools/
│   └── validate_results.py                           # valida run_id e join exp↔sys
├── docs/
│   ├── atividade.md                                   # enunciado da atividade
│   ├── virtualbox-tutorial.pdf                        # tutorial de uso da VM
│   └── cloud/                                         # runbook OpenStack + nginx/systemd
└── src/
    ├── cpu-bound/      ┐
    ├── memory-bound/   ├ cada projeto tem: app/ k6/ metrics/collectors/ plots/
    └── io-bound/       ┘ docs/ (deploy_vm.sh, run_vm_experiments.sh) data/ tests/
```

Cada projeto tem seu próprio README com instruções detalhadas de execução local, geração de carga (k6), coleta de métricas e experimentos na VM.

## Execução local (resumo)

### No Linux / VM (Bash)
```bash
cd src/cpu-bound          # ou src/memory-bound / src/io-bound
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run_local_sweep.sh      # sobe o servidor, coleta métricas, executa carga e gera os gráficos
```

Por padrão a varredura usa `WORKERS=$(nproc)` (multi-worker, um processo por
núcleo) e `REQUEST_MIX=uniform`. Para comparar com a linha de base de processo
único ou variar o método HTTP:
```bash
WORKERS=1 ./run_local_sweep.sh
REQUEST_MIX=mixed ./run_local_sweep.sh
```
Cada nível recebe um `run_id` único gravado no CSV do experimento e no do
monitor, permitindo o join exato entre demanda e recursos.

### No Windows (Git Bash ou PowerShell)
1. **Criar e ativar o ambiente:**
   - No Git Bash:
     ```bash
     cd src/cpu-bound     # ou src/memory-bound / src/io-bound
     python -m venv .venv
     source .venv/Scripts/activate
     pip install -r requirements.txt
     ./run_local_sweep.sh
     ```
   - No PowerShell:
     ```powershell
     cd src\cpu-bound
     python -m venv .venv
     .\.venv\Scripts\Activate.ps1
     pip install -r requirements.txt
     uvicorn app.main:app --host 127.0.0.1 --port 8000
     ```

Requisitos: Python 3.12, [k6](https://k6.io/) instalado no host.

### Testes automatizados

Com o `.venv` do projeto ativo (que já tem `fastapi`/`psutil`), instale as
dependências de teste e rode o pytest a partir da raiz do repositório:

```bash
pip install -r requirements-dev.txt
pytest -q
```

Os testes cobrem os três backends (fumaça da API) e o join/validação por
`run_id` nos CSVs.

## Experimentos na VM (VirtualBox)

O experimento segue a metodologia onde **a aplicação e o monitoramento de infraestrutura rodam na VM**, enquanto **a geração de carga (k6) ocorre externamente a partir da máquina física (host ou LCC)**.

### 1. Na Máquina Virtual (Deploy da Aplicação)
Acesse a VM (via terminal local ou SSH), navegue até a pasta do perfil que deseja testar e execute o script de deploy automatizado:

```bash
cd poi-virtualbox-backend/src/cpu-bound   # ou memory-bound / io-bound
bash docs/deploy_vm.sh
```
*O que o script faz:*
- Instala pacotes do sistema e cria o ambiente virtual Python (`.venv`).
- Configura e inicializa o serviço no `systemd` (`poi-cpu.service`, `poi-memory.service` ou `poi-io.service`) escutando na porta `8000`.

> **Atenção:** Se for alternar entre os projetos na mesma VM, pare o serviço anterior antes de subir o próximo (`sudo systemctl stop poi-cpu`, etc.), pois todos utilizam a porta `8000`.

---

### 2. Na Máquina Host / Externa (Geração de Carga e Coleta de Métricas)
No seu computador físico (fora da VM), certifique-se de que a VM está acessível pela rede e rode o executor de experimentos apontando para o IP configurado:

- **Rede em modo NAT com Port Forwarding (porta 8000 HTTP e 2222 SSH):**
  ```bash
  cd src/cpu-bound
  VM_IP=127.0.0.1 VM_PORT=2222 SCENARIO=C1_1vcpu_1gib ./docs/run_vm_experiments.sh
  ```

- **Rede em modo Placa em modo Bridge ou Host-Only (IP próprio na rede local):**
  ```bash
  cd src/cpu-bound
  VM_IP=192.168.56.10 VM_PORT=22 VM_USER=osboxes SCENARIO=C1_1vcpu_1gib ./docs/run_vm_experiments.sh
  ```

*O que esse script faz por fora:*
1. Testa a conectividade com o endpoint `/health` da VM.
2. Dispara a carga externa de requisições com o **k6** para cada nível de VU.
3. Conecta via SSH na VM em paralelo para capturar as métricas reais de CPU, memória e disco diretamente do guest (`system_monitor.py`).
4. Faz o download automático dos CSVs de métricas (`sys_vm_*.csv`) para a pasta `data/results/`.

O orquestrador deriva `VCPUS`/`RAM_GIB` do nome do cenário (ex.:
`C2_2vcpu_1gib`) e usa `WORKERS=VCPUS` por padrão — um worker uvicorn por vCPU.
Use `WORKERS=1` apenas para a linha de base explícita de processo único.

---

### 3. Variação dos Cenários de Provisionamento
Para atender ao requisito da atividade (avaliar a relação entre demanda e recursos):
1. Execute a bateria de testes para o cenário base (**C1: 1 vCPU / 1 GiB RAM**).
2. Desligue a VM (`sudo poweroff`).
3. No VirtualBox, altere os recursos em **Configurações → Sistema** (ex.: vCPUs para 2 ou 4, RAM para 2 GiB ou 4 GiB).
4. Ligue a VM e repita o teste com o respectivo rótulo de cenário (`SCENARIO=C2_2vcpu_1gib`, `SCENARIO=C3_2vcpu_2gib`, etc.).

---

### 4. Geração dos Gráficos Comparativos
Após executar os cenários, gere os gráficos consolidados (dentro do projeto):
```bash
python plots/generate_plots.py --results-dir data/results --out-dir plots/output
```
São geradas **duas figuras agrupadas**, com eixos fixos no máximo global de cada
métrica para permitir comparação entre níveis e execuções:
- `overview_demand.png`: demanda × latência, vazão, CPU, memória, I/O e eficiência.
- `overview_time.png`: CPU/memória do sistema e do processo ao longo do tempo.

### 5. Validação dos Resultados
```bash
python3 tools/validate_results.py src/*/data/results
```
O validador confere se cada experimento novo tem `run_id`, se ele é único e se
existe um `sys_*.csv` com o mesmo `run_id`. CSVs antigos sem `run_id` são
tratados como *legacy* e não quebram a validação.

## Deploy em Nuvem (OpenStack/LSD)

O roteiro completo, com comandos manuais e prints para o relatório, está em
[`docs/cloud/README.md`](docs/cloud/README.md). A arquitetura recomendada usa
**uma VM por grupo**, as três aplicações simultâneas e nginx na frente:

```text
LB poi-atividades
  TCP 22  -> VM:22 (SSH)
  HTTP 80 -> VM:80 (nginx)
               /cpu/    -> 127.0.0.1:8000
               /memory/ -> 127.0.0.1:8001
               /io/     -> 127.0.0.1:8002
               /health  -> 200 OK

VM: volume OpenStack de 1 GB montado em /mnt/poi-data
    logs/, io/, results/
```

Arquivos de apoio:

- `docs/cloud/nginx-poi.conf` — reverse proxy por path;
- `docs/cloud/systemd/poi-cpu.service`, `poi-memory.service`, `poi-io.service`;
- `docs/cloud/logrotate-poi.conf` — rotação dos logs no volume.

As apps escutam apenas em `127.0.0.1`; o LB fala com o nginx na porta 80. O
`run_vm_experiments.sh` aceita `SSH_HOST`/`SSH_PORT`, `TARGET_URL`, `SERVICE`,
`REMOTE_APP_DIR` e `REMOTE_RESULTS_DIR` para operar atrás do LB.

## Resultados

- Resultados brutos: `src/*/data/results/` (CSVs do k6 e `sys_vm_*` da VM).
- Gráficos comparativos: `src/*/plots/output/`.
- Relatório: [Relatório](docs/relatorio-link.txt) com base nos resultados obtidos.