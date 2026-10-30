# Relatório — Atividade 2: Deploy em Nuvem IaaS (OpenStack/LSD)

**Grupo:** `<número/identificador>`
**Integrantes:** `<nome — matrícula>`; `<nome — matrícula>`
**Data:** `<dd/mm/aaaa>`
**Ambiente:** Nuvem do LSD — projeto Provisionamento
**Branch do código:** `feat/deploy-cloud`
**Roteiro técnico:** `docs/cloud/README.md`

> Este arquivo pertence à branch `report/atividade-cloud`, que será apagada ao
> final. Ele é apenas o esqueleto do relatório: substitua os placeholders,
> insira os prints e mantenha as explicações de “o que está acontecendo” em cada
> etapa.

---

## 1. Objetivo e arquitetura

Descreva o objetivo da atividade: migrar as três aplicações da atividade
anterior (CPU-bound, memory-bound e IO-bound) para uma VM na nuvem IaaS,
configurando acesso, armazenamento em volume e balanceamento de carga.

Arquitetura implantada:

```text
Cliente/k6
   |
   v
LB poi-atividades (por grupo)
   |-- TCP 22  -> VM:22 (SSH)
   |-- HTTP 80 -> VM:80 (nginx)
                    |-- /cpu/    -> 127.0.0.1:8000
                    |-- /memory/ -> 127.0.0.1:8001
                    |-- /io/     -> 127.0.0.1:8002
                    `-- /health  -> 200

VM general.small / ubuntu-24.04
   /mnt/poi-data (volume 1 GB)
      logs/, io/, results/
```

Explique por que as três apps rodam na mesma VM (restrição de uma VM por grupo)
e por que o nginx foi usado (conflito de `/health`/`/info`, um único listener
HTTP no LB).

**Evidência:** `[PRINT 01 — diagrama/arquitetura]`

---

## 2. Primeira parte

### 2.1 Acesso à CLI do OpenStack

Descreva como o acesso foi feito (VDI `/local/uasc/provisionamento` ou máquina
local com `openrc`), qual projeto foi selecionado e como a autenticação foi
validada.

```bash
source ~/openrc
openstack token issue
openstack project show "$OS_PROJECT_NAME"
```

**Evidência:** `[PRINT 02 — token/projeto]`

### 2.2 Inventário de flavors, imagens, servidores e snapshots

Cole os comandos e explique o que cada listagem mostra. Registre o flavor
`general.small` (vCPU/RAM) e a imagem `ubuntu-24.04`.

```bash
openstack flavor list
openstack flavor show general.small -f yaml
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

**Evidências:** `[PRINT 03 — flavors]`, `[PRINT 04 — imagens]`,
`[PRINT 05 — servidores/volumes/snapshots]`.

### 2.3 Par de chaves SSH

Explique a diferença entre chave pública e privada e por que a privada não vai
para a nuvem.

```bash
ssh-keygen -t ed25519 -f "$SSH_KEY" -C "$KEY_NAME"
openstack keypair create --public-key "${SSH_KEY}.pub" "$KEY_NAME"
openstack keypair list
```

**Evidência:** `[PRINT 06 — keypair criado]`

### 2.4 Criação da VM

Registre o comando de criação, o flavor, a imagem, a rede e o security group.
Explique o `--wait` e o significado do status `ACTIVE`.

```bash
openstack server create \
  --flavor general.small \
  --image ubuntu-24.04 \
  --key-name "$KEY_NAME" \
  --security-group "$SG_NAME" \
  --network "<REDE>" \
  --wait \
  "$VM_NAME"

openstack server show "$VM_NAME" -c status -c addresses -f yaml
```

**Evidência:** `[PRINT 07 — server create/show]`

### 2.5 Security group

Liste as regras criadas e explique o princípio de menor privilégio: SSH e HTTP
somente a partir da sub-rede/VIP do LB; portas 8000–8002 fechadas para fora.

```bash
openstack security group rule list "$SG_NAME"
```

**Evidência:** `[PRINT 08 — regras do SG]`

### 2.6 Balanceador: acesso SSH

Mostre a descoberta do LB, do listener TCP, do pool e do membro. Explique o
health monitor TCP e o status `ONLINE`.

```bash
openstack loadbalancer list
openstack loadbalancer show "$LB_NAME" -f yaml
openstack loadbalancer listener list --loadbalancer "$LB_NAME"
openstack loadbalancer pool list
openstack loadbalancer member list <POOL>
openstack loadbalancer status show "$LB_NAME"
```

Se o LB tiver IP flutuante, mostre a associação e use esse endereço como
`LB_VIP`.

**Evidências:** `[PRINT 09 — LB/listener/pool]`, `[PRINT 10 — status ONLINE]`.

---

## 3. Segunda parte

### 3.1 Acesso SSH à VM pelo LB

Mostre o comando e o prompt dentro da VM. Registre `nproc`, `free -h` e
`lsb_release -a`.

```bash
ssh -i "$SSH_KEY" -p "$LB_SSH_PORT" "${VM_USER}@${LB_VIP}"
nproc
free -h
lsb_release -a
```

**Evidência:** `[PRINT 11 — SSH e recursos da VM]`

### 3.2 Volume de 1 GB: criação, attach e montagem

Mostre a criação/anexo pela CLI e, dentro da VM, `lsblk`, `blkid`, `mount`,
`df -h` e `findmnt`. Explique o `mkfs.ext4`, o UUID no `/etc/fstab` e o
`nofail`.

```bash
openstack volume create --size 1 "$VOLUME_NAME"
openstack server add volume "$VM_NAME" "$VOLUME_NAME"
openstack volume show "$VOLUME_NAME" -c status -c attachments -f yaml

# Na VM:
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINT
sudo mkfs.ext4 -L poi-data /dev/vdb
sudo mkdir -p /mnt/poi-data
sudo mount /dev/vdb /mnt/poi-data
UUID=$(sudo blkid -s UUID -o value /dev/vdb)
echo "UUID=${UUID} /mnt/poi-data ext4 defaults,nofail 0 2" | sudo tee -a /etc/fstab
sudo mount -a
findmnt /mnt/poi-data
```

**Evidências:** `[PRINT 12 — volume create/attach]`,
`[PRINT 13 — lsblk/blkid]`, `[PRINT 14 — df/findmnt]`.

### 3.3 Deploy simultâneo das três aplicações

Explique a divisão em `/opt/poi/{cpu,memory,io}`, os venvs independentes e as
portas 8000/8001/8002 em loopback. Mostre os `systemctl status` e o `ss -ltnp`.

```bash
sudo systemctl status poi-cpu poi-memory poi-io --no-pager
ss -ltnp | grep -E '8000|8001|8002'
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8001/health
curl -s http://127.0.0.1:8002/health
```

**Evidências:** `[PRINT 15 — status das três unidades]`,
`[PRINT 16 — portas/curl local]`.

### 3.4 Logs e dados no volume

Mostre que os logs estão em `/mnt/poi-data/logs/` e que o io-bound usa
`IO_DIR=/mnt/poi-data/io`. Explique o `StandardOutput=append`, o
`RequiresMountsFor` e o `logrotate` com `copytruncate`.

```bash
ls -l /mnt/poi-data/logs/
tail -n 20 /mnt/poi-data/logs/poi-cpu.log
df -h /mnt/poi-data
sudo logrotate -d /etc/logrotate.d/poi
```

**Evidências:** `[PRINT 17 — logs no volume]`,
`[PRINT 18 — df/logrotate]`.

### 3.5 nginx como entrada única

Explique o `location /cpu/`, `/memory/`, `/io/` e o `proxy_pass` com barra no
final (remoção do prefixo). Mostre `nginx -t` e os `curl` locais.

```bash
sudo nginx -t
curl -s http://127.0.0.1/health
curl -s http://127.0.0.1/cpu/health
curl -s http://127.0.0.1/memory/health
curl -s http://127.0.0.1/io/health
```

**Evidências:** `[PRINT 19 — nginx -t/status]`,
`[PRINT 20 — curl pelos prefixos]`.

### 3.6 Balanceador: acesso HTTP às aplicações

Mostre o listener HTTP, o pool para a porta 80 do nginx, o membro e o health
check em `/health`. Valide os três prefixos pelo VIP do LB.

```bash
openstack loadbalancer status show "$LB_NAME"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/health"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/cpu/health"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/memory/health"
curl -s "http://${LB_VIP}:${LB_HTTP_PORT}/io/health"
```

**Evidências:** `[PRINT 21 — LB HTTP status]`,
`[PRINT 22 — curl pelo VIP]`.

### 3.7 Snapshot da VM e do volume

Mostre o snapshot do servidor e o snapshot do volume. Explique que o snapshot
do servidor cobre o disco raiz e **não** inclui o volume anexado; por isso o
volume também foi snapshotado.

```bash
openstack server stop "$VM_NAME"
openstack server image create --name "${PREFIX}-snapshot" --wait "$VM_NAME"
openstack server start "$VM_NAME"
openstack volume snapshot create --volume "$VOLUME_NAME" --name "${PREFIX}-data-snapshot"
openstack image list | grep "${PREFIX}"
openstack volume snapshot list
```

**Evidências:** `[PRINT 23 — snapshot da VM]`,
`[PRINT 24 — snapshot do volume]`.

---

## 4. Experimentos da atividade anterior na nuvem (opcional)

Se o grupo rerodar os experimentos, registre:
- flavor real (`VCPUS`, `RAM_GIB`) e `WORKERS` usados;
- `TARGET_URL` via LB (`/cpu`, `/memory`, `/io`);
- se as outras apps foram paradas durante cada teste;
- comparação LB vs túnel SSH direto (overhead);
- CSVs e gráficos gerados (`overview_demand.png`, `overview_time.png`).

**Evidências:** `[PRINT 25 — execução do run_vm_experiments.sh]`,
`[PRINT 26 — gráfico/CSV]`.

---

## 5. Dificuldades e decisões

Descreva problemas reais encontrados, por exemplo:
- portas 22/80 já ocupadas no LB compartilhado;
- necessidade de IP flutuante para alcançar o VIP;
- health check do LB falhando por security group;
- volume aparecendo como `/dev/vdb` vs `/dev/sdb`;
- limites de memória com as três apps na mesma VM;
- logs enchendo o volume de 1 GB.

Explique como cada problema foi diagnosticado e resolvido.

---

## 6. Conclusão

Retome o que foi comprovado:
- a VM na nuvem hospeda as três aplicações simultaneamente;
- o LB é o único ponto de entrada (SSH e HTTP);
- o volume de 1 GB é usado por logs, I/O e resultados;
- o snapshot permite reproduzir o ambiente (VM + volume);
- quais limitações/overheads a nuvem introduziu em relação ao VirtualBox.

---

## Apêndice A — Checklist de prints

- [ ] 01 Arquitetura
- [ ] 02 `token issue`/projeto
- [ ] 03 `flavor list`/`show general.small`
- [ ] 04 `image list`
- [ ] 05 `server`/`volume`/`snapshot` list
- [ ] 06 `keypair`
- [ ] 07 `server create/show`
- [ ] 08 `security group rule list`
- [ ] 09 LB listener/pool/member
- [ ] 10 LB `status show`
- [ ] 11 SSH + `nproc`/`free -h`
- [ ] 12 `volume create/add`
- [ ] 13 `lsblk`/`blkid`
- [ ] 14 `df`/`findmnt`
- [ ] 15 `systemctl status poi-*`
- [ ] 16 `ss`/`curl` local
- [ ] 17 logs no volume
- [ ] 18 `df`/`logrotate`
- [ ] 19 `nginx -t`/status
- [ ] 20 `curl` pelos prefixos
- [ ] 21 LB HTTP status
- [ ] 22 `curl` pelo VIP
- [ ] 23 snapshot da VM
- [ ] 24 snapshot do volume
- [ ] 25 (opcional) experimento
- [ ] 26 (opcional) gráfico/CSV

## Apêndice B — Comandos completos

Os comandos manuais detalhados estão em `docs/cloud/README.md`. Cole aqui
apenas os trechos usados pelo grupo, com os valores reais (sem senhas).
