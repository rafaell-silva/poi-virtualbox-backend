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

### No Linux / VM (Bash)
```bash
cd src/cpu-bound          # ou src/memory-bound / src/io-bound
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run_local_sweep.sh      # sobe o servidor, coleta métricas, executa carga e gera os gráficos
```

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

---

### 3. Variação dos Cenários de Provisionamento
Para atender ao requisito da atividade (avaliar a relação entre demanda e recursos):
1. Execute a bateria de testes para o cenário base (**C1: 1 vCPU / 1 GiB RAM**).
2. Desligue a VM (`sudo poweroff`).
3. No VirtualBox, altere os recursos em **Configurações → Sistema** (ex.: vCPUs para 2 ou 4, RAM para 2 GiB ou 4 GiB).
4. Ligue a VM e repita o teste com o respectivo rótulo de cenário (`SCENARIO=C2_2vcpu_1gib`, `SCENARIO=C3_2vcpu_2gib`, etc.).

---

### 4. Geração dos Gráficos Comparativos
Após executar os cenários, gere os gráficos consolidados:
```bash
python plots/generate_plots.py
```
Os gráficos comparativos de latência, vazão (RPS), saturação de CPU e uso de memória serão gerados em `plots/output/`.

## Resultados

- Resultados brutos: `src/*/data/results/` (CSVs do k6 e `sys_vm_*` da VM).
- Gráficos comparativos: `src/*/plots/output/`.
- Relatório: [Relatório](docs/relatorio-link.txt) com base nos resultados obtidos.