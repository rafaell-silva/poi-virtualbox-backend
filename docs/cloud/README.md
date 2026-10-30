# Deploy na Nuvem IaaS (OpenStack/LSD) — Atividade 2

Este diretório reúne o roteiro **manual** para repetir, na Nuvem do LSD
(projeto Provisionamento), o deploy das três aplicações da atividade anterior.
Os comandos foram pensados para serem executados um a um e servirem de
evidência (prints) no relatório.

> **Escopo deste roteiro.** Ele cobre a infraestrutura (VM, chave, security
> group, volume e balanceador) e o deploy das três aplicações na **mesma VM**,
> com nginx na frente. A criação dos recursos OpenStack é manual de propósito:
> não há script que “esconda” as etapas exigidas na atividade.
>
> Os arquivos de apoio são:
>
> - `nginx-poi.conf` — reverse proxy com `/cpu/`, `/memory/` e `/io/`;
> - `systemd/poi-*.service` — unidades das três aplicações;
> - `logrotate-poi.conf` — rotação dos logs no volume.

---

## 0. Arquitetura proposta

```text
Cliente / k6
    |
    v
LB poi-atividades (por grupo)
    |-- TCP 22  -> VM:22  (SSH)
    |-- HTTP 80 -> VM:80  (nginx)
                         |-- /cpu/     -> 127.0.0.1:8000 (poi-cpu)
                         |-- /memory/  -> 127.0.0.1:8001 (poi-memory)
                         |-- /io/      -> 127.0.0.1:8002 (poi-io)
                         `-- /health  -> 200 OK

VM (general.small + ubuntu-24.04)
    /mnt/poi-data  <- volume OpenStack de 1 GB
        logs/poi-cpu.log
        logs/poi-memory.log
        logs/poi-io.log
        logs/nginx/
        io/          <- IO_DIR do io-bound
        results/     <- CSVs do monitor remoto
```

Por que nginx: só há **uma VM por grupo**, mas as três aplicações usam rotas
conflitantes (`/health`, `/info`). O nginx resolve isso com prefixos de path e
permite que o LB tenha **um único listener HTTP** apontando para a VM.

---

## 1. Variáveis do grupo

Defina os nomes abaixo no seu shell (ou apenas substitua nos comandos). Use algo
único para não conflitar com outros grupos no projeto compartilhado.

```bash
GROUP=XX
PREFIX="poi-g${GROUP}"
VM_NAME="${PREFIX}-vm"
KEY_NAME="${PREFIX}-key"
SG_NAME="${PREFIX}-sg"
VOLUME_NAME="${PREFIX}-data"
SSH_KEY="$HOME/.ssh/${KEY_NAME}"
LB_NAME="poi-atividades"

# Portas do LB. Se 22/80 já estiverem em uso no LB compartilhado, escolha
# portas únicas e use-as no SSH e no TARGET_URL.
LB_SSH_PORT=22
LB_HTTP_PORT=80

# Usuário padrão das imagens Ubuntu na nuvem (diferente do osboxes do VirtualBox).
VM_USER=ubuntu
```

> **O que está acontecendo:** todos os recursos recebem o prefixo do grupo. Isso
> evita colisões de nome em um projeto OpenStack compartilhado e deixa claro, nos
> prints, o que pertence ao grupo.

---

## 2. Acesso à CLI do OpenStack

O acesso pode ser feito pela VDI indicada no enunciado (`/local/uasc/provisionamento`)
ou por uma máquina com o cliente `openstack` instalado e o arquivo `openrc`
fornecido pelo professor.

```bash
# Carregue as credenciais (o nome do arquivo pode variar).
source ~/openrc

# Confirme que a autenticação funciona.
openstack token issue
openstack project show "$OS_PROJECT_NAME"
```

> **O que está acontecendo:** o `openrc` exporta `OS_AUTH_URL`, `OS_USERNAME`,
> `OS_PASSWORD`, `OS_PROJECT_NAME` etc. O `token issue` prova que a CLI está
> autenticada antes de criar qualquer recurso.

### 2.1 Inventário inicial (prints obrigatórios)

```bash
openstack flavor list
openstack image list
openstack server list
openstack volume list
openstack volume snapshot list
openstack keypair list
openstack security group list
openstack network list
openstack subnet list
openstack loadbalancer list
```

Descubra os dados que serão usados no restante:

```bash
openstack flavor show general.small -f yaml
openstack image show ubuntu-24.04 -f yaml
openstack network list -f yaml
```

> **O que está acontecendo:** o flavor define vCPU/RAM (e, portanto, o
> `WORKERS`); a imagem define o usuário SSH; a rede/sub-rede define onde a VM e
> o LB ficam. Guarde esses valores: eles entram no relatório e no `run_id` dos
> experimentos (`VCPUS`, `RAM_GIB`, `WORKERS`).

---

## 3. Par de chaves SSH

Gere a chave **no host** (ou na VDI) e registre apenas a parte pública no
OpenStack:

```bash
ssh-keygen -t ed25519 -f "$SSH_KEY" -C "$KEY_NAME"
chmod 600 "$SSH_KEY"

openstack keypair create --public-key "${SSH_KEY}.pub" "$KEY_NAME"
openstack keypair list
```

> **O que está acontecendo:** a chave privada fica só com você; o OpenStack
> injeta a pública na VM no primeiro boot (cloud-init). Sem ela, não há como
> autenticar via SSH.

---

## 4. Security group

Crie um grupo dedicado e libere **apenas** o necessário. O SSH e o HTTP chegam
pelo LB, então as regras podem ser restritas à sub-rede do balanceador.

```bash
openstack security group create "$SG_NAME" \
  --description "POI ${GROUP}: SSH/HTTP via LB, saida liberada"

# Descubra a sub-rede/VIP do LB (se ele já existir) para restringir as regras.
openstack loadbalancer show "$LB_NAME" -c vip_address -c vip_subnet_id -f yaml

# Exemplo: liberar SSH e HTTP a partir da sub-rede do LB.
LB_SUBNET_CIDR="<CIDR_DA_SUBNET_DO_LB>"
openstack security group rule create --protocol tcp --dst-port 22 \
  --remote-ip "$LB_SUBNET_CIDR" "$SG_NAME"
openstack security group rule create --protocol tcp --dst-port 80 \
  --remote-ip "$LB_SUBNET_CIDR" "$SG_NAME"

# ICMP ajuda no diagnóstico (opcional).
openstack security group rule create --protocol icmp \
  --remote-ip "$LB_SUBNET_CIDR" "$SG_NAME"
```

Se precisar acessar a VM diretamente para o primeiro setup (antes do LB), abra
temporariamente SSH a partir do seu IP e **remova a regra depois**:

```bash
MY_IP=$(curl -s https://ifconfig.me)
openstack security group rule create --protocol tcp --dst-port 22 \
  --remote-ip "${MY_IP}/32" "$SG_NAME"
# ... ao terminar:
openstack security group rule list "$SG_NAME"
# openstack security group rule delete <ID>
```

> **O que está acontecendo:** o security group é um firewall stateful aplicado à
> porta da VM. As portas 8000–8002 **não** precisam ser abertas: o nginx fala
> com elas em `127.0.0.1`.

---

## 5. Criar a VM

Use o flavor e a imagem pedidos na atividade:

```bash
openstack server create \
  --flavor general.small \
  --image ubuntu-24.04 \
  --key-name "$KEY_NAME" \
  --security-group "$SG_NAME" \
  --network "<REDE>" \
  --wait \
  "$VM_NAME"
```

Descubra o IP privado e a sub-rede da VM:

```bash
openstack server show "$VM_NAME" -c addresses -c status -f yaml
openstack port list --server "$VM_NAME" -c ID -c "Fixed IP Addresses" -f yaml
# Anote o IP (VM_IP) e o subnet_id do IP fixo.
```

> **O que está acontecendo:** o `--network` conecta a VM à rede do projeto; o
> `--key-name` injeta a chave; o `--security-group` aplica o firewall. O IP
> retornado é privado — o acesso externo será pelo LB.

Se a rede não tiver rota para a internet e você precisar instalar pacotes,
crie/associe um IP flutuante temporariamente:

```bash
openstack floating ip create <REDE_EXTERNA>
openstack server add floating ip "$VM_NAME" "<IP_FLUTUANTE>"
```

---

## 6. Balanceador: acesso SSH

O LB `poi-atividades` é **por grupo**. Primeiro verifique o que já existe:

```bash
openstack loadbalancer list
openstack loadbalancer show "$LB_NAME" -f yaml
openstack loadbalancer listener list --loadbalancer "$LB_NAME"
openstack loadbalancer pool list
openstack loadbalancer member list <POOL>
```

### 6.1 Se o LB ainda não tem listener TCP para SSH

```bash
openstack loadbalancer listener create \
  --name "${PREFIX}-ssh" \
  --protocol TCP \
  --protocol-port "$LB_SSH_PORT" \
  --loadbalancer "$LB_NAME"

openstack loadbalancer pool create \
  --name "${PREFIX}-ssh-pool" \
  --protocol TCP \
  --lb-algorithm ROUND_ROBIN \
  --listener "${PREFIX}-ssh"

openstack loadbalancer member create \
  --name "${PREFIX}-vm-ssh" \
  --address "<VM_IP>" \
  --protocol-port 22 \
  --subnet-id "<SUBNET_ID_DA_VM>" \
  "${PREFIX}-ssh-pool"

openstack loadbalancer healthmonitor create \
  --name "${PREFIX}-ssh-hm" \
  --type TCP \
  --delay 5 --timeout 3 --max-retries 3 \
  --pool "${PREFIX}-ssh-pool"
```

### 6.2 Se já existir um listener/pool TCP para SSH

Não crie outro: apenas adicione a VM como membro do pool existente.

```bash
openstack loadbalancer member create \
  --name "${PREFIX}-vm-ssh" \
  --address "<VM_IP>" \
  --protocol-port 22 \
  --subnet-id "<SUBNET_ID_DA_VM>" \
  "<POOL_EXISTENTE>"
```

### 6.3 Descobrir o VIP e validar

```bash
openstack loadbalancer show "$LB_NAME" -c vip_address -c vip_port_id -f yaml
openstack loadbalancer status show "$LB_NAME"
```

### 6.4 VIP privado x IP flutuante

O `vip_address` do LB costuma ser um IP **privado** da rede do projeto. Para
acessar de fora da nuvem (host pessoal/VDI), normalmente existe um **IP
flutuante** associado à porta VIP:

```bash
VIP_PORT_ID=$(openstack loadbalancer show "$LB_NAME" -c vip_port_id -f value)
openstack floating ip list --port "$VIP_PORT_ID" -f yaml
```

Use o endereço do floating IP como `LB_VIP` nos próximos comandos. Se não houver
nenhum, execute o roteiro de dentro da VDI/rede do LSD ou peça ao professor para
associar um IP flutuante ao VIP do LB.

```bash
# Exemplo: associar um IP flutuante novo ao VIP do LB (se permitido).
openstack floating ip create <REDE_EXTERNA>
openstack floating ip set --port "$VIP_PORT_ID" "<IP_FLUTUANTE>"
```

> **O que está acontecendo:** o listener TCP escuta na porta `LB_SSH_PORT` do
> VIP e encaminha para a porta 22 da VM. O health monitor TCP só marca o membro
> como `ONLINE` quando o `sshd` responde.
>
> **Se as portas 22/80 já estiverem ocupadas** no LB compartilhado, use portas
> únicas (ex.: `2222`, `8080`) e reflita isso em `SSH_PORT`/`TARGET_URL`.

---

## 7. Acessar a VM via SSH (pelo LB)

```bash
# LB_VIP: use o floating IP do LB (seção 6.4) ou o vip_address dentro da nuvem.
LB_VIP="<IP_FLUTUANTE_OU_VIP>"

ssh -i "$SSH_KEY" -p "$LB_SSH_PORT" \
  -o ServerAliveInterval=30 \
  "${VM_USER}@${LB_VIP}"
```

Dentro da VM, confirme os recursos e a versão do Ubuntu:

```bash
nproc
free -h
lsb_release -a
```

> **O que está acontecendo:** o SSH não vai direto ao IP privado; ele entra no
> VIP do LB, que encaminha para o membro do pool. O `nproc` define o valor de
> `WORKERS` usado pelas aplicações e registrado no `run_id`.

---

## 8. Volume de 1 GB: criar, anexar e montar

### 8.1 Criar e anexar (no host, com a CLI)

```bash
openstack volume create --size 1 "$VOLUME_NAME"
openstack volume list
openstack server add volume "$VM_NAME" "$VOLUME_NAME"
openstack volume show "$VOLUME_NAME" -c status -c attachments -f yaml
```

### 8.2 Identificar o device dentro da VM

```bash
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT
sudo blkid
```

O volume novo normalmente aparece como `/dev/vdb` (ou `/dev/sdb`). **Confirme
pelo tamanho (1G) e pelo fato de não ter filesystem/mountpoint.** Não formate o
disco raiz.

### 8.3 Formatar e montar (apenas se o volume estiver vazio)

```bash
DEVICE=/dev/vdb   # ajuste para o device identificado no passo anterior

# ATENÇÃO: apaga o conteúdo do device. Só rode se ele for o volume novo.
sudo mkfs.ext4 -L poi-data "$DEVICE"

sudo mkdir -p /mnt/poi-data
sudo mount "$DEVICE" /mnt/poi-data
mountpoint /mnt/poi-data
df -h /mnt/poi-data
```

### 8.4 Montagem persistente via UUID

```bash
UUID=$(sudo blkid -s UUID -o value "$DEVICE")
echo "UUID=${UUID} /mnt/poi-data ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo mount -a
findmnt /mnt/poi-data
```

> **O que está acontecendo:** o `fstab` garante que o volume volte montado após
> reboot. `nofail` evita que a VM entre em emergência se o volume não estiver
> disponível. As unidades systemd usam `RequiresMountsFor=/mnt/poi-data`, então
> as aplicações só sobem com o volume montado.

### 8.5 Diretórios da aplicação no volume

```bash
sudo chown -R "${USER}:${USER}" /mnt/poi-data
mkdir -p /mnt/poi-data/logs/nginx /mnt/poi-data/io /mnt/poi-data/results
ls -ld /mnt/poi-data/*
```

---

## 9. Deploy das três aplicações (simultâneas)

### 9.1 Pacotes do sistema

```bash
sudo apt-get update -q
sudo apt-get install -y python3 python3-venv python3-pip nginx rsync curl git
```

### 9.2 Copiar os projetos do host para a VM

No **host**, a partir da raiz do repositório:

```bash
for app in cpu memory io; do
  ssh -i "$SSH_KEY" -p "$LB_SSH_PORT" "${VM_USER}@${LB_VIP}" \
    "sudo mkdir -p /opt/poi/${app} && sudo chown -R ${VM_USER}:${VM_USER} /opt/poi"

  rsync -av --delete \
    --exclude '.git' --exclude '.venv' --exclude 'data/results' \
    -e "ssh -i ${SSH_KEY} -p ${LB_SSH_PORT}" \
    "src/${app}-bound/" "${VM_USER}@${LB_VIP}:/opt/poi/${app}/"
done
```

> **O que está acontecendo:** cada projeto continua autocontido em
> `/opt/poi/{cpu,memory,io}`. O `data/results` local não é copiado; os resultados
> da nuvem ficarão no volume.

### 9.3 Criar os virtualenvs

Na VM:

```bash
for app in cpu memory io; do
  cd "/opt/poi/${app}"
  python3 -m venv .venv
  .venv/bin/pip install --upgrade pip --quiet
  .venv/bin/pip install fastapi "uvicorn[standard]" psutil --quiet
done
```

> **O que está acontecendo:** cada app tem seu próprio venv para não misturar
> dependências. Não é necessário instalar `matplotlib` na VM; os gráficos são
> gerados no host, a partir dos CSVs baixados.

### 9.4 Definir o número de workers

```bash
nproc
sudo mkdir -p /etc/poi
echo "WORKERS=$(nproc)" | sudo tee /etc/poi/cloud.env
```

Os templates systemd leem `/etc/poi/cloud.env` e usam esse valor em
`--workers ${WORKERS}`.

> **O que está acontecendo:** `WORKERS` é o número de processos uvicorn. Em uma
> VM de N vCPUs, o padrão é N. O valor também deve ser passado como `WORKERS`
> nos experimentos para aparecer no `run_id`.

### 9.5 Instalar as unidades systemd

No host, envie os templates; na VM, copie para `/etc/systemd/system/`:

```bash
for svc in poi-cpu poi-memory poi-io; do
  scp -i "$SSH_KEY" -P "$LB_SSH_PORT" \
    "docs/cloud/systemd/${svc}.service" "${VM_USER}@${LB_VIP}:/tmp/"
  ssh -i "$SSH_KEY" -p "$LB_SSH_PORT" "${VM_USER}@${LB_VIP}" \
    "sudo cp /tmp/${svc}.service /etc/systemd/system/"
done
```

Ajuste, se necessário, `User`, `WorkingDirectory`, portas e limites de memória.
Depois:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now poi-cpu poi-memory poi-io
systemctl status poi-cpu poi-memory poi-io --no-pager
ss -ltnp | grep -E '8000|8001|8002'
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8001/health
curl -s http://127.0.0.1:8002/health
tail -n 20 /mnt/poi-data/logs/poi-cpu.log
```

> **O que está acontecendo:** as três apps escutam apenas em `127.0.0.1`
> (8000/8001/8002). O acesso externo é feito pelo nginx. Os logs vão para o
> volume; `RequiresMountsFor` impede que o serviço suba sem o volume.

---

## 10. nginx: um único ponto HTTP para as três apps

No host, envie o arquivo:

```bash
scp -i "$SSH_KEY" -P "$LB_SSH_PORT" \
  docs/cloud/nginx-poi.conf "${VM_USER}@${LB_VIP}:/tmp/nginx-poi.conf"
```

Na VM:

```bash
sudo cp /tmp/nginx-poi.conf /etc/nginx/sites-available/poi
sudo ln -sf /etc/nginx/sites-available/poi /etc/nginx/sites-enabled/poi
sudo rm -f /etc/nginx/sites-enabled/default
sudo mkdir -p /mnt/poi-data/logs/nginx
sudo nginx -t
sudo systemctl reload nginx
```

Teste localmente:

```bash
curl -s http://127.0.0.1/health
curl -s http://127.0.0.1/cpu/health
curl -s http://127.0.0.1/memory/health
curl -s http://127.0.0.1/io/health
curl -s "http://127.0.0.1/cpu/info"
```

> **O que está acontecendo:** o `proxy_pass http://poi_cpu/;` com barra no final
> remove o prefixo `/cpu/` antes de encaminhar para a app. Assim
> `http://LB/cpu/process-image` chega como `/process-image` na aplicação.

---

## 11. Balanceador: acesso HTTP às aplicações

Crie (ou reutilize) um listener HTTP. O pool aponta para a **porta 80 do
nginx**, não para as portas das apps.

### 11.1 Listener novo

```bash
openstack loadbalancer listener create \
  --name "${PREFIX}-http" \
  --protocol HTTP \
  --protocol-port "$LB_HTTP_PORT" \
  --loadbalancer "$LB_NAME"

openstack loadbalancer pool create \
  --name "${PREFIX}-http-pool" \
  --protocol HTTP \
  --lb-algorithm ROUND_ROBIN \
  --listener "${PREFIX}-http"

openstack loadbalancer member create \
  --name "${PREFIX}-vm-http" \
  --address "<VM_IP>" \
  --protocol-port 80 \
  --subnet-id "<SUBNET_ID_DA_VM>" \
  "${PREFIX}-http-pool"

openstack loadbalancer healthmonitor create \
  --name "${PREFIX}-http-hm" \
  --type HTTP \
  --url-path /health \
  --http-method GET \
  --expected-codes 200 \
  --delay 5 --timeout 3 --max-retries 3 \
  --pool "${PREFIX}-http-pool"
```

### 11.2 Listener/pool existente

Se o LB já tem um listener HTTP no projeto, adicione o membro ao pool existente:

```bash
openstack loadbalancer member create \
  --name "${PREFIX}-vm-http" \
  --address "<VM_IP>" \
  --protocol-port 80 \
  --subnet-id "<SUBNET_ID_DA_VM>" \
  "<POOL_EXISTENTE>"
```

### 11.3 Validar pelo VIP

```bash
# LB_VIP: use o floating IP do LB (seção 6.4) ou o vip_address dentro da nuvem.
LB_VIP="<IP_FLUTUANTE_OU_VIP>"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/health"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/cpu/health"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/memory/health"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/io/health"
openstack loadbalancer status show "$LB_NAME"
```

> **O que está acontecendo:** o LB faz health check em `/health` (nginx) e
> encaminha o tráfego HTTP para o nginx, que distribui por path. O SG da VM
> precisa permitir a porta 80 a partir da sub-rede do LB.

---

## 12. Snapshot da VM e do volume

A atividade pede o snapshot da VM configurada. O snapshot do servidor cobre o
**disco raiz**; o volume anexado é um recurso separado e precisa do seu próprio
snapshot.

```bash
# Consistência: pare a VM antes do snapshot do servidor.
openstack server stop "$VM_NAME"
openstack server image create --name "${PREFIX}-snapshot" --wait "$VM_NAME"
openstack server start "$VM_NAME"

# Snapshot do volume de dados (logs, io, results).
openstack volume snapshot create \
  --volume "$VOLUME_NAME" \
  --name "${PREFIX}-data-snapshot"
```

Verifique:

```bash
openstack image list | grep "${PREFIX}"
openstack volume snapshot list
openstack server show "$VM_NAME" -c status -c image -f yaml
openstack volume show "$VOLUME_NAME" -c status -c attachments -f yaml
```

> **O que está acontecendo:** `server image create` gera uma imagem a partir do
> disco raiz da VM; ela pode ser usada para criar outra VM com o mesmo sistema.
> O volume não entra nessa imagem. O `volume snapshot` preserva os dados do
> volume (logs, diretório de I/O e resultados).

---

## 13. Rodar os experimentos da atividade anterior na nuvem (opcional)

O orquestrador foi parametrizado para aceitar SSH e HTTP em endpoints
diferentes, além de diretório remoto e nome de serviço.

No **host**, a partir de `src/cpu-bound` (e depois `memory-bound`/`io-bound`):

```bash
# Descubra os recursos do flavor para preencher os metadados.
openstack flavor show general.small -c vcpus -c ram -f yaml

cd src/cpu-bound

TARGET_URL="http://${LB_VIP}:${LB_HTTP_PORT}/cpu" \
SSH_HOST="$LB_VIP" \
SSH_PORT="$LB_SSH_PORT" \
SERVICE=poi-cpu \
REMOTE_APP_DIR=/opt/poi/cpu \
REMOTE_RESULTS_DIR=/mnt/poi-data/results \
SCENARIO=cloud_general.small \
VCPUS=<vcpus_do_flavor> \
RAM_GIB=<ram_gib_do_flavor> \
WORKERS=<nproc_da_vm> \
./docs/run_vm_experiments.sh
```

Para evitar interferência das outras apps:

```bash
ssh -i "$SSH_KEY" -p "$LB_SSH_PORT" "${VM_USER}@${LB_VIP}" \
  "sudo systemctl stop poi-memory poi-io"
# ... rode o experimento de CPU ...
ssh -i "$SSH_KEY" -p "$LB_SSH_PORT" "${VM_USER}@${LB_VIP}" \
  "sudo systemctl start poi-memory poi-io"
```

Repita trocando `cpu` por `memory`/`io`, o `SERVICE`, o `REMOTE_APP_DIR` e o
prefixo do `TARGET_URL`.

### 13.1 Comparar LB vs acesso direto (opcional, mas recomendado)

As apps escutam em `127.0.0.1`; use um túnel SSH para medir sem o LB:

```bash
ssh -N -i "$SSH_KEY" -p "$LB_SSH_PORT" \
  -L 18000:127.0.0.1:8000 "${VM_USER}@${LB_VIP}"

# Em outro terminal:
cd src/cpu-bound
TARGET_URL=http://127.0.0.1:18000 \
SSH_HOST="$LB_VIP" SSH_PORT="$LB_SSH_PORT" \
SERVICE=poi-cpu REMOTE_APP_DIR=/opt/poi/cpu \
REMOTE_RESULTS_DIR=/mnt/poi-data/results \
SCENARIO=cloud_direct_general.small \
VCPUS=<vcpus> RAM_GIB=<ram> WORKERS=<nproc> \
./docs/run_vm_experiments.sh
```

> **O que está acontecendo:** o primeiro teste passa pelo LB; o segundo usa um
> túnel SSH até a porta local da app. A diferença de latência/RPS é o overhead
> do balanceador e da rede da nuvem.

---

## 14. Checklist de evidências para o relatório

Marque cada item com um print e uma frase explicando o que ele prova.

### Primeira parte

- [ ] `openstack token issue` / projeto autenticado.
- [ ] `openstack flavor list` e `openstack flavor show general.small`.
- [ ] `openstack image list` (incluindo `ubuntu-24.04`).
- [ ] `openstack server list` (antes/depois).
- [ ] `openstack volume list` e `openstack volume snapshot list`.
- [ ] `openstack keypair list`.
- [ ] `openstack security group list` + `security group rule list`.
- [ ] `openstack server create ...` e `server show` (status `ACTIVE`).
- [ ] LB: `listener list`, `pool list`, `member list`, `status show`.
- [ ] `ssh -p <LB_SSH_PORT> ubuntu@<LB_VIP>` funcionando.

### Segunda parte

- [ ] `openstack volume create`, `server add volume`, `volume show`.
- [ ] Na VM: `lsblk`, `blkid`, `mount`, `df -h /mnt/poi-data`, `findmnt`.
- [ ] `systemctl status poi-cpu poi-memory poi-io`.
- [ ] `ss -ltnp` mostrando 8000/8001/8002 em loopback.
- [ ] `nginx -t` e `systemctl status nginx`.
- [ ] `curl` local nas três apps.
- [ ] `curl` pelo VIP do LB nas três apps.
- [ ] `tail` dos logs em `/mnt/poi-data/logs/`.
- [ ] `openstack loadbalancer status show poi-atividades`.
- [ ] `openstack server image create` + `image list`.
- [ ] `openstack volume snapshot create` + `volume snapshot list`.
- [ ] (Opcional) execução de um experimento com `run_vm_experiments.sh` e
      CSV/gráfico gerado.

---

## 15. Troubleshooting

| Sintoma | Verificar |
|---|---|
| `openstack` não autentica | `source openrc`; `openstack token issue`; projeto correto |
| VM não sobe | `openstack server show`; quota; imagem/rede; `server console log` |
| SSH pelo LB não conecta | listener TCP, pool, membro `ONLINE`, SG liberando 22 da sub-rede do LB, `sshd` ativo |
| Health check do LB falha | `curl` no `/health` local; SG liberando 80; membro no pool; `openstack loadbalancer status show` |
| App não sobe | `journalctl -u poi-*`; log no volume; porta ocupada; venv/dependências |
| Volume não monta | `lsblk`, `blkid`, `/etc/fstab`, `journalctl -xe`; não formate disco com dados |
| Logs enchem o volume | `logrotate-poi.conf`; reduzir `--log-level`; `--no-access-log` em experimentos |
| Memória estoura | reduzir `WORKERS`, `MAX_CONCURRENT_MIB`; não rodar as três apps sob carga ao mesmo tempo |
| LB não permite criar listener | usar listener/pool existente; escolher porta única; falar com o professor/administrador |

---

## 16. Observações de metodologia

- O LB adiciona latência e um novo ponto de falha; registre no relatório que os
  números da nuvem não são diretamente comparáveis aos do VirtualBox.
- Rode o k6 de uma máquina próxima à nuvem (VDI/LSD), não de casa, para reduzir
  ruído de rede.
- Para cada perfil, pare as outras duas apps ou registre que elas estavam
  ociosas. O `run_id` já grava `profile`, `scenario`, `vcpus`, `ram_gib`,
  `workers`, `request_mix` e `rep`.
- O `WORKERS` usado no deploy deve ser o mesmo informado ao
  `run_vm_experiments.sh`; caso contrário, o `run_id` não descreve a execução.
- Snapshot da VM ≠ snapshot do volume: mostre os dois no relatório e explique a
  diferença.
