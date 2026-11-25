# Orquestração com Kubernetes (Minikube) na Nuvem OpenStack/LSD — Atividade 5 (Parte 1)

Este diretório reúne o roteiro completo e os comandos passo a passo para
provisionar a infraestrutura da **Atividade 5 (Parte 1)** na nuvem OpenStack do
LSD, instalar um cluster **Kubernetes com Minikube**, realizar o deploy das três
aplicações (`cpu-bound`, `memory-bound` e `io-bound`) utilizando configurações
de workload adequadas e expô-las externamente através do **Load Balancer** do
OpenStack.

Os comandos foram estruturados para serem executados um a um, facilitando a
captura de evidências (prints) para o relatório e o preenchimento do formulário de
entrega.

> **Arquivos de apoio no repositório:**
>
> - [`k8s/namespace.yaml`](../../k8s/namespace.yaml) — Namespace dedicado `poi`.
> - [`k8s/cpu-bound/deployment.yaml`](../../k8s/cpu-bound/deployment.yaml) e [`k8s/cpu-bound/service.yaml`](../../k8s/cpu-bound/service.yaml)
> - [`k8s/memory-bound/deployment.yaml`](../../k8s/memory-bound/deployment.yaml) e [`k8s/memory-bound/service.yaml`](../../k8s/memory-bound/service.yaml)
> - [`k8s/io-bound/statefulset.yaml`](../../k8s/io-bound/statefulset.yaml) e [`k8s/io-bound/service.yaml`](../../k8s/io-bound/service.yaml)
> - [`k8s/nginx/nginx-poi-k8s.conf.template`](../../k8s/nginx/nginx-poi-k8s.conf.template) — template de reverse proxy na VM.
> - [`k8s/scripts/install-minikube.sh`](../../k8s/scripts/install-minikube.sh), [`configure-nginx-proxy.sh`](../../k8s/scripts/configure-nginx-proxy.sh) e [`verify-k8s.sh`](../../k8s/scripts/verify-k8s.sh).

---

## 0. Arquitetura proposta

```text
Cliente externo (Browser / curl / k6)
    |
    v
Load Balancer OpenStack (poi-atividades — VIP / Floating IP)
    |-- TCP 22  (ou LB_SSH_PORT)  --> VM:22  (SSH)
    `-- HTTP 80 (ou LB_HTTP_PORT) --> VM:80  (Nginx reverse proxy)
                                          |-- /health  --> 200 OK (health check do LB)
                                          |-- /cpu/    --> MINIKUBE_IP:30080
                                          |-- /memory/ --> MINIKUBE_IP:30081
                                          `-- /io/     --> MINIKUBE_IP:30082

VM OpenStack (Ubuntu 22.04/24.04 LTS | 4 vCPUs | 8 GB RAM)
    `-- Minikube (--driver=docker, 3 vCPUs, 6144 MiB RAM)
          `-- Namespace: poi
                |-- Service poi-cpu-service    (NodePort 30080) --> Deployment poi-cpu    (2 réplicas)
                |-- Service poi-memory-service (NodePort 30081) --> Deployment poi-memory (2 réplicas)
                `-- Service poi-io-service     (NodePort 30082) --> StatefulSet poi-io   (1 réplica + PVC 1Gi)
                    `-- Headless Service poi-io-headless (clusterIP: None)
```

**Por que usar Nginx na VM atrás do Load Balancer?**
- As três aplicações possuem rotas conflitantes na raiz (`/health`, `/info`).
- Com o Minikube rodando via driver `docker` dentro da VM, os `NodePort`s ficam acessíveis no IP interno da rede Docker do Minikube (`minikube ip`).
- O Nginx na porta `80` da VM unifica as três aplicações sob prefixos de path (`/cpu/`, `/memory/`, `/io/`), entrega `/health` para o Health Monitor do Load Balancer do OpenStack e evita ter que abrir múltiplos listeners HTTP ou reconfigurar regras de L7 no Octavia.

---

## 1. Variáveis do grupo

Defina as variáveis abaixo no shell da máquina de controle (onde a CLI `openstack` está configurada) para padronizar os nomes e evitar colisões com outros grupos no projeto compartilhado:

```bash
GROUP=XX                        # Substitua pelo número/identificador do seu grupo
PREFIX="poi-g${GROUP}"
VM_NAME="${PREFIX}-k8s-vm"
KEY_NAME="${PREFIX}-key"        # Reutilize a chave da atividade anterior ou crie uma nova
SG_NAME="${PREFIX}-k8s-sg"
SSH_KEY="$HOME/.ssh/${KEY_NAME}"
LB_NAME="poi-atividades"        # Nome do Load Balancer utilizado nas atividades anteriores

# Portas no Load Balancer (ajuste se já estiverem em uso no LB compartilhado)
LB_SSH_PORT=22
LB_HTTP_PORT=80

# Usuário padrão das imagens Ubuntu Cloud no OpenStack
VM_USER=ubuntu
```

---

## 2. Limpeza da infraestrutura anterior no OpenStack

> **ATENÇÃO CRÍTICA:** O projeto OpenStack é compartilhado com outros grupos e com o professor. **NUNCA remova recursos pertencentes ao professor ou a outros grupos** (observe sempre o nome/prefixo antes de deletar qualquer recurso).

Autentique-se na CLI do OpenStack e faça o inventário dos recursos existentes:

```bash
source ~/openrc
openstack token issue

openstack server list
openstack volume list
openstack volume snapshot list
openstack loadbalancer list
openstack security group list
```

Se houver recursos antigos **do seu grupo** das atividades anteriores que precisem ser liberados para respeitar as cotas do projeto:

### 2.1 Remover membros antigos do Load Balancer (se aplicável)

```bash
# Liste listeners e pools do LB para identificar apenas os do seu grupo
openstack loadbalancer listener list --loadbalancer "$LB_NAME"
openstack loadbalancer pool list

# Liste os membros de um pool específico do seu grupo
openstack loadbalancer member list <POOL_DO_GRUPO>

# Remova apenas o membro apontando para a VM antiga do grupo
openstack loadbalancer member delete <POOL_DO_GRUPO> <MEMBER_ANTIGO_ID>
```

> Se o grupo criou um listener/pool próprio na atividade anterior e deseja recriá-lo ou reutilizá-lo, basta atualizar/remover o `member` antigo e cadastrar a nova VM no mesmo pool (ou criar novos caso tenham sido deletados).

### 2.2 Remover VM, volumes e snapshots antigos do grupo

```bash
# Verifique o nome e ID exatos antes de apagar!
openstack server delete <ID_OU_NOME_VM_ANTIGA_DO_GRUPO> --wait

# Caso exista volume/snapshot antigo do seu grupo que não seja mais necessário:
openstack volume snapshot delete <SNAPSHOT_ANTIGO_DO_GRUPO>
openstack volume delete <VOLUME_ANTIGO_DO_GRUPO>
```

---

## 3. Provisionamento da nova VM no OpenStack (4 vCPUs, 8 GB RAM, Ubuntu 22.04/24.04)

Requisitos mínimos da atividade:
- **Sistema operacional:** Ubuntu Server 22.04 ou 24.04 LTS
- **vCPUs:** 4
- **Memória RAM:** 8 GB

### 3.1 Descobrir Flavor, Imagem e Rede

```bash
openstack flavor list
openstack image list
openstack network list
```

Escolha:
- Um `flavor` com pelo menos **4 vCPUs e 8192 MB de RAM** (ex.: `general.medium` ou equivalente listado).
- A imagem **Ubuntu 22.04** ou **Ubuntu 24.04** disponível no projeto.
- A rede privada do projeto (`<REDE>`).

Confirme os detalhes antes de criar:

```bash
FLAVOR_NAME="<FLAVOR_4VCPU_8GB>"
IMAGE_NAME="<IMAGEM_UBUNTU_22_OU_24>"
NETWORK_NAME="<REDE_DO_PROJETO>"

openstack flavor show "$FLAVOR_NAME" -c name -c vcpus -c ram -c disk -f yaml
openstack image show "$IMAGE_NAME" -c id -c name -c status -f yaml
```

### 3.2 Par de chaves SSH (caso não reutilize o anterior)

```bash
openstack keypair list

# Se precisar criar uma nova chave:
ssh-keygen -t ed25519 -f "$SSH_KEY" -C "$KEY_NAME"
chmod 600 "$SSH_KEY"
openstack keypair create --public-key "${SSH_KEY}.pub" "$KEY_NAME"
```

### 3.3 Security Group

Crie um Security Group liberando apenas SSH (22) e HTTP (80) a partir da sub-rede do Load Balancer (e opcionalmente ICMP):

```bash
openstack security group create "$SG_NAME" \
  --description "POI ${GROUP} K8s: SSH/HTTP via LB"

# Descubra o CIDR da sub-rede do Load Balancer
VIP_SUBNET_ID=$(openstack loadbalancer show "$LB_NAME" -c vip_subnet_id -f value)
LB_SUBNET_CIDR=$(openstack subnet show "$VIP_SUBNET_ID" -c cidr -f value)
echo "VIP_SUBNET_ID=$VIP_SUBNET_ID | LB_SUBNET_CIDR=$LB_SUBNET_CIDR"

openstack security group rule create --protocol tcp --dst-port 22 \
  --remote-ip "$LB_SUBNET_CIDR" "$SG_NAME"
openstack security group rule create --protocol tcp --dst-port 80 \
  --remote-ip "$LB_SUBNET_CIDR" "$SG_NAME"
openstack security group rule create --protocol icmp \
  --remote-ip "$LB_SUBNET_CIDR" "$SG_NAME"

openstack security group rule list "$SG_NAME"
```

### 3.4 Criar a VM e registrar o ID e IP privado

```bash
openstack server create \
  --flavor "$FLAVOR_NAME" \
  --image "$IMAGE_NAME" \
  --key-name "$KEY_NAME" \
  --security-group "$SG_NAME" \
  --network "$NETWORK_NAME" \
  --wait \
  "$VM_NAME"
```

Capture o **ID da VM** (necessário no formulário de entrega!), o IP privado e a `subnet_id`:

```bash
openstack server show "$VM_NAME" -c id -c name -c status -c flavor -c image -c addresses -f yaml
openstack port list --server "$VM_NAME" -c ID -c "Fixed IP Addresses" -f yaml

VM_ID=$(openstack server show "$VM_NAME" -c id -f value)
echo "ID da VM (guardar para o formulário): $VM_ID"
```

---

## 4. Configuração do Load Balancer OpenStack

Inspecione o Load Balancer existente:

```bash
openstack loadbalancer show "$LB_NAME" -c id -c name -c vip_address -c vip_port_id -c vip_subnet_id -c operating_status -f yaml
openstack loadbalancer listener list --loadbalancer "$LB_NAME"
openstack loadbalancer pool list
```

Descubra o IP externo (Floating IP associado ao VIP do LB, ou o próprio VIP se o acesso for interno à rede do LSD):

```bash
VIP_PORT_ID=$(openstack loadbalancer show "$LB_NAME" -c vip_port_id -f value)
openstack floating ip list --port "$VIP_PORT_ID" -f yaml
```

Defina no shell:

```bash
VM_IP="<IP_PRIVADO_DA_VM>"
SUBNET_ID="<SUBNET_ID_DA_VM>"
LB_VIP="<FLOATING_IP_OU_VIP_DO_LB>"
```

### 4.1 Listener TCP para SSH (se ainda não existir)

Se o listener/pool TCP de SSH do grupo ainda não existir, crie-o:

```bash
openstack loadbalancer listener create \
  --name "${PREFIX}-k8s-ssh" \
  --protocol TCP \
  --protocol-port "$LB_SSH_PORT" \
  --loadbalancer "$LB_NAME" --wait

openstack loadbalancer pool create \
  --name "${PREFIX}-k8s-ssh-pool" \
  --protocol TCP \
  --lb-algorithm ROUND_ROBIN \
  --listener "${PREFIX}-k8s-ssh" --wait

openstack loadbalancer healthmonitor create \
  --name "${PREFIX}-k8s-ssh-hm" \
  --type TCP \
  --delay 5 --timeout 3 --max-retries 3 \
  --pool "${PREFIX}-k8s-ssh-pool" --wait
```

Adicione a nova VM como membro do pool SSH (novo ou já existente):

```bash
SSH_POOL_NAME="${PREFIX}-k8s-ssh-pool"   # ou o nome do pool SSH existente do grupo
openstack loadbalancer member create \
  --name "${PREFIX}-k8s-vm-ssh" \
  --address "$VM_IP" \
  --protocol-port 22 \
  --subnet-id "$SUBNET_ID" \
  "$SSH_POOL_NAME" --wait
```

### 4.2 Listener HTTP para as aplicações (se ainda não existir)

Se o listener/pool HTTP do grupo ainda não existir, crie-o:

```bash
openstack loadbalancer listener create \
  --name "${PREFIX}-k8s-http" \
  --protocol HTTP \
  --protocol-port "$LB_HTTP_PORT" \
  --loadbalancer "$LB_NAME" --wait

openstack loadbalancer pool create \
  --name "${PREFIX}-k8s-http-pool" \
  --protocol HTTP \
  --lb-algorithm ROUND_ROBIN \
  --listener "${PREFIX}-k8s-http" --wait

openstack loadbalancer healthmonitor create \
  --name "${PREFIX}-k8s-http-hm" \
  --type HTTP \
  --url-path /health \
  --http-method GET \
  --expected-codes 200 \
  --delay 5 --timeout 3 --max-retries 3 \
  --pool "${PREFIX}-k8s-http-pool" --wait
```

Adicione a nova VM (porta `80`, onde o Nginx vai escutar) como membro do pool HTTP:

```bash
HTTP_POOL_NAME="${PREFIX}-k8s-http-pool"   # ou o nome do pool HTTP existente do grupo
openstack loadbalancer member create \
  --name "${PREFIX}-k8s-vm-http" \
  --address "$VM_IP" \
  --protocol-port 80 \
  --subnet-id "$SUBNET_ID" \
  "$HTTP_POOL_NAME" --wait

openstack loadbalancer status show "$LB_NAME"
```

### 4.3 Acessar a VM via SSH pelo Load Balancer

```bash
ssh -i "$SSH_KEY" -p "$LB_SSH_PORT" \
  -o ServerAliveInterval=30 \
  "${VM_USER}@${LB_VIP}"
```

Dentro da VM, valide os recursos provisionados (ótimo print para o relatório):

```bash
nproc
free -h
df -h /
lsb_release -a
```

---

## 5. Instalação e configuração do Kubernetes com Minikube na VM

Dentro da VM, clone o repositório do grupo (na branch com os arquivos da Atividade 5):

```bash
git clone <URL_DO_REPOSITORIO_GITHUB> ~/poi-virtualbox-backend
cd ~/poi-virtualbox-backend
```

### 5.1 Instalar Docker, kubectl e Minikube

Você pode executar o script automatizado ou rodar os comandos manualmente para registrar prints de cada etapa:

**Opção A — via script (idempotente):**

```bash
./k8s/scripts/install-minikube.sh
newgrp docker
```

**Opção B — comandos manuais (passo a passo):**

```bash
# 1. Docker Engine + conntrack
sudo apt-get update -q
sudo apt-get install -y curl ca-certificates apt-transport-https conntrack docker.io
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker

# 2. kubectl
KUBECTL_VERSION=$(curl -fsSL https://dl.k8s.io/release/stable.txt)
curl -fsSL -o /tmp/kubectl "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 /tmp/kubectl /usr/local/bin/kubectl
rm -f /tmp/kubectl

# 3. Minikube
curl -fsSL -o /tmp/minikube-linux-amd64 https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install -o root -g root -m 0755 /tmp/minikube-linux-amd64 /usr/local/bin/minikube
rm -f /tmp/minikube-linux-amd64

# 4. Verificar versões
docker --version
kubectl version --client
minikube version
```

### 5.2 Iniciar o cluster Minikube

Reservamos **3 vCPUs** e **6144 MiB (6 GiB) de RAM** para o nó do Minikube, deixando **1 vCPU e ~2 GiB de RAM** livres para o sistema operacional da VM, daemon Docker e Nginx:

```bash
minikube start \
  --driver=docker \
  --cpus=3 \
  --memory=6144 \
  --addons=metrics-server
```

Valide que o cluster está saudável e ativo:

```bash
minikube status
minikube ip
kubectl cluster-info
kubectl get nodes -o wide
kubectl get pods -A
```

---

## 6. Justificativa das escolhas de Workload e Exposição

A atividade solicita justificar as escolhas de workload do Kubernetes para cada aplicação:

| Aplicação | Workload escolhido | Réplicas | Exposição | Justificativa |
|---|---|---|---|---|
| `cpu-bound` | `Deployment` | 2 | `Service` (`NodePort: 30080`) | Aplicação **stateless**: não mantém estado nem grava arquivos em disco entre requisições. Um `Deployment` permite escalar réplicas facilmente (`replicas: 2`), distribuir requisições entre os pods via `Service` e fazer atualizações sem indisponibilidade (`RollingUpdate` com `maxUnavailable: 0`). |
| `memory-bound` | `Deployment` | 2 | `Service` (`NodePort: 30081`) | Aplicação **stateless**: aloca buffers `bytearray` em memória apenas durante a requisição e os libera ao final. O `Deployment` gerencia o ciclo de vida com `readinessProbe`/`livenessProbe` e limites rígidos (`limits.memory: 768Mi` > `MAX_CONCURRENT_MIB: 512`) para evitar que um pod cause OOMKill no nó inteiro. |
| `io-bound` | `StatefulSet` + `volumeClaimTemplates` | 1 | `Headless Service` + `Service` (`NodePort: 30082`) | Aplicação **stateful em relação ao armazenamento**: grava e lê arquivos reais com `os.fsync()` em `IO_DIR=/data/io`. O `StatefulSet` com `volumeClaimTemplates` garante identidade estável (`poi-io-0`) e um `PersistentVolumeClaim` (`io-data` de 1 GiB) dedicado, evitando `tmpfs`/camada efêmera do container e preservando o perfil de disco real. |

### Detalhes importantes da configuração

- **Segurança (`securityContext`)**: todos os containers principais rodam como usuário não-root (`runAsUser: 1000`, `appuser` definido nos Dockerfiles) com `allowPrivilegeEscalation: false`.
- **`initContainer` no `StatefulSet` (`poi-io`)**: o provisioner `hostpath` padrão do Minikube cria o diretório do PVC pertencente a `root:root`. O `initContainer` (`fix-io-dir-permissions`) executa `chown -R 1000:1000 /data/io` antes de iniciar o container da aplicação, evitando `PermissionError` ao escrever no volume.
- **Dimensionamento de recursos (`requests` / `limits`)** dentro dos 3 vCPUs / 6 GiB do Minikube:
  - `poi-cpu` (2 réplicas): requests `250m` CPU / `128Mi` RAM; limits `1000m` CPU / `256Mi` RAM.
  - `poi-memory` (2 réplicas): requests `150m` CPU / `256Mi` RAM; limits `500m` CPU / `768Mi` RAM.
  - `poi-io` (1 réplica): requests `150m` CPU / `128Mi` RAM; limits `500m` CPU / `512Mi` RAM.
  - Total de requests das aplicações: `950m` CPU (< 3000m) e `896Mi` RAM, deixando ampla margem para o control plane do Kubernetes, `metrics-server` e picos de carga.
- **Exposição via `NodePort` + Nginx**: como o Minikube usa `--driver=docker` dentro da VM OpenStack, os Services `NodePort` (`30080`, `30081`, `30082`) ficam acessíveis pelo IP da rede Docker do Minikube (`minikube ip`). O Nginx na porta `80` da VM faz o roteamento por prefixo (`/cpu/`, `/memory/`, `/io/`) e responde `/health` para o Load Balancer OpenStack.

---

## 7. Deploy das três aplicações no Kubernetes

A partir da raiz do repositório dentro da VM (`~/poi-virtualbox-backend`):

### 7.1 Criar o Namespace

```bash
kubectl apply -f k8s/namespace.yaml
kubectl get namespace poi
```

### 7.2 Deploy e exposição da Aplicação 1 — CPU-bound

```bash
kubectl apply -f k8s/cpu-bound/deployment.yaml
kubectl apply -f k8s/cpu-bound/service.yaml
kubectl -n poi rollout status deployment/poi-cpu
```

### 7.3 Deploy e exposição da Aplicação 2 — Memory-bound

```bash
kubectl apply -f k8s/memory-bound/deployment.yaml
kubectl apply -f k8s/memory-bound/service.yaml
kubectl -n poi rollout status deployment/poi-memory
```

### 7.4 Deploy e exposição da Aplicação 3 — IO-bound

```bash
kubectl apply -f k8s/io-bound/statefulset.yaml
kubectl apply -f k8s/io-bound/service.yaml
kubectl -n poi rollout status statefulset/poi-io
```

### 7.5 Inspecionar todos os recursos criados

```bash
kubectl -n poi get all -o wide
kubectl -n poi get pvc
kubectl -n poi describe pod -l app=poi-io
```

Teste direto nos NodePorts do Minikube (antes mesmo do Nginx) e observe o balanceamento entre os pods em `/info` da app `cpu-bound`:

```bash
MINIKUBE_IP=$(minikube ip)
curl -s "http://${MINIKUBE_IP}:30080/health"
for i in 1 2 3 4; do curl -s "http://${MINIKUBE_IP}:30080/info"; echo ""; done

curl -s "http://${MINIKUBE_IP}:30081/health"
curl -s "http://${MINIKUBE_IP}:30082/health"
```

Verifique também os logs dos pods:

```bash
kubectl -n poi logs -l app=poi-cpu --tail=20
kubectl -n poi logs -l app=poi-memory --tail=20
kubectl -n poi logs statefulset/poi-io --tail=20
```

---

## 8. Configuração e validação do acesso externo

### 8.1 Configurar o Nginx reverse proxy na VM

Execute o script de configuração do Nginx:

```bash
./k8s/scripts/configure-nginx-proxy.sh
```

Valide o Nginx e o acesso local pela porta `80` da VM:

```bash
sudo systemctl status nginx --no-pager
curl -s http://127.0.0.1/health
curl -s http://127.0.0.1/cpu/health && echo ""
curl -s http://127.0.0.1/memory/health && echo ""
curl -s http://127.0.0.1/io/health && echo ""
```

### 8.2 Validar o acesso externo via IP do Load Balancer OpenStack

No **seu computador local** (fora da VM) ou da máquina de controle, execute os testes apontando para `LB_VIP` (o IP público/flutuante do Load Balancer OpenStack):

```bash
LB_VIP="<IP_DO_LOAD_BALANCER>"
LB_HTTP_PORT=80   # ajuste se usar outra porta

# 1. Health check do Load Balancer / Nginx e de cada aplicação
curl -i "http://${LB_VIP}:${LB_HTTP_PORT}/health"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/cpu/health" && echo ""
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/memory/health" && echo ""
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/io/health" && echo ""

# 2. Info das 3 aplicações (verifique os pods respondendo em /cpu/info)
for i in 1 2 3 4; do curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/cpu/info"; echo ""; done
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/memory/info" && echo ""
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/io/info" && echo ""

# 3. Execução real dos endpoints de workload
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/cpu/process-image?intensity=500" && echo ""
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/memory/workload?size_mib=8&dwell_ms=100" && echo ""
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/io/process?size_mb=4&iterations=1" && echo ""
```

Dentro da VM, você também pode rodar o verificador completo:

```bash
LB_VIP="<IP_DO_LOAD_BALANCER>" LB_HTTP_PORT=80 ./k8s/scripts/verify-k8s.sh
```

Por fim, confirme no OpenStack que o membro HTTP no Load Balancer está `ONLINE`:

```bash
openstack loadbalancer status show "$LB_NAME"
```

---

## 9. Checklist de evidências (prints) para o relatório

### Infraestrutura OpenStack
- [ ] Inventário inicial e limpeza dos recursos antigos do grupo (`openstack server list`, `openstack volume list`, `openstack loadbalancer list`).
- [ ] Criação do Security Group e listagem das regras (`openstack security group rule list`).
- [ ] Criação da nova VM (4 vCPUs, 8 GB RAM, Ubuntu 22.04/24.04) e `openstack server show` exibindo o **ID da VM** e status `ACTIVE`.
- [ ] Configuração do Load Balancer (`listener list`, `pool list`, `member list`, `loadbalancer status show` com membros `ONLINE`).
- [ ] Acesso SSH pelo Load Balancer e validação de recursos na VM (`nproc`, `free -h`, `lsb_release -a`).

### Cluster Kubernetes (Minikube)
- [ ] Instalação de Docker, `kubectl` e `minikube` (`docker --version`, `kubectl version --client`, `minikube version`).
- [ ] Inicialização do cluster (`minikube start --driver=docker --cpus=3 --memory=6144 --addons=metrics-server`).
- [ ] Estado do cluster (`minikube status`, `kubectl cluster-info`, `kubectl get nodes -o wide`, `kubectl get pods -A`).

### Deploy e Exposição no Kubernetes
- [ ] Aplicação dos manifestos (`kubectl apply` do namespace, Deployments, StatefulSet e Services).
- [ ] `kubectl -n poi get all -o wide` e `kubectl -n poi get pvc` mostrando todos os pods `Running` (`1/1` Ready) e PVC `Bound`.
- [ ] Logs dos pods (`kubectl -n poi logs ...`).
- [ ] Configuração e validação do Nginx na VM (`configure-nginx-proxy.sh`, `nginx -t`, `systemctl status nginx`).
- [ ] Testes externos pelo IP do Load Balancer (`curl` em `/cpu/info`, `/cpu/process-image`, `/memory/info`, `/memory/workload`, `/io/info`, `/io/process` ou print no navegador).

---

## 10. Resumo para o formulário de entrega

Substitua `<OWNER>/<REPO>`, `<BRANCH>`, `<VM_ID>` e `<LB_VIP>` pelos valores reais do grupo ao preencher o formulário:

| Campo do formulário | Valor / Modelo |
|---|---|
| **URL do GitHub** | `https://github.com/<OWNER>/<REPO>` |
| **ID da VM criada** | `<UUID retornado por openstack server show "$VM_NAME" -c id -f value>` |
| **URL YAML de configuração — App 1 (cpu-bound)** | `https://github.com/<OWNER>/<REPO>/blob/<BRANCH>/k8s/cpu-bound/deployment.yaml` |
| **URL YAML de configuração — App 2 (memory-bound)** | `https://github.com/<OWNER>/<REPO>/blob/<BRANCH>/k8s/memory-bound/deployment.yaml` |
| **URL YAML de configuração — App 3 (io-bound)** | `https://github.com/<OWNER>/<REPO>/blob/<BRANCH>/k8s/io-bound/statefulset.yaml` |
| **URL YAML de exposição — App 1 (cpu-bound)** | `https://github.com/<OWNER>/<REPO>/blob/<BRANCH>/k8s/cpu-bound/service.yaml` |
| **URL YAML de exposição — App 2 (memory-bound)** | `https://github.com/<OWNER>/<REPO>/blob/<BRANCH>/k8s/memory-bound/service.yaml` |
| **URL YAML de exposição — App 3 (io-bound)** | `https://github.com/<OWNER>/<REPO>/blob/<BRANCH>/k8s/io-bound/service.yaml` |
| **URL da App 1 em execução (cpu-bound)** | `http://<LB_VIP>/cpu/info` (ou `/cpu/process-image`) |
| **URL da App 2 em execução (memory-bound)** | `http://<LB_VIP>/memory/info` (ou `/memory/workload`) |
| **URL da App 3 em execução (io-bound)** | `http://<LB_VIP>/io/info` (ou `/io/process`) |
| **URL da imagem no Docker Hub — App 1** | `https://hub.docker.com/r/rafaellsb/poi-cpu-bound` |
| **URL da imagem no Docker Hub — App 2** | `https://hub.docker.com/r/rafaellsb/poi-memory-bound` |
| **URL da imagem no Docker Hub — App 3** | `https://hub.docker.com/r/rafaellsb/poi-io-bound` |

---

## 11. Troubleshooting

| Sintoma | Causa provável e como resolver |
|---|---|
| `permission denied` ao rodar `minikube start` | O usuário ainda não recarregou o grupo `docker`. Rode `newgrp docker` ou faça logout/login no SSH. |
| Após reiniciar a VM, apps retornam `502 Bad Gateway` no Nginx | O Minikube pode ter parado ou mudado de IP. Rode `minikube start` e depois `./k8s/scripts/configure-nginx-proxy.sh` para atualizar o IP nos upstreams. |
| Pod `poi-io-0` em `CrashLoopBackOff` com `PermissionError` | Verifique se o `initContainer` `fix-io-dir-permissions` rodou corretamente (`kubectl -n poi logs poi-io-0 -c fix-io-dir-permissions`). |
| Pod `poi-memory-*` com status `OOMKilled` | O consumo excedeu `limits.memory: 768Mi`. Reduza `MAX_CONCURRENT_MIB` ou aumente o limite de memória em `k8s/memory-bound/deployment.yaml` dentro da capacidade do Minikube. |
| Health monitor do Load Balancer marca membro HTTP como `ERROR`/`OFFLINE` | Confirme que o Nginx responde `200` em `http://127.0.0.1/health` dentro da VM e que o Security Group libera TCP `80` a partir do `LB_SUBNET_CIDR`. |
| Timeout ao acessar externamente via `LB_VIP` | Confirme as regras do Security Group (portas 22 e 80 liberadas para a sub-rede do LB) e se os listeners/pools/membros no `openstack loadbalancer status show` estão `ONLINE`. |
