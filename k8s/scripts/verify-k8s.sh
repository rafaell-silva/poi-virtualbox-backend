#!/usr/bin/env bash
set -euo pipefail

# Verifica o estado dos recursos no namespace 'poi', os NodePorts no Minikube,
# o proxy Nginx local e (opcionalmente) o acesso externo via Load Balancer.
#
# Uso:
#   ./k8s/scripts/verify-k8s.sh
#   LB_VIP=<IP_DO_LB> LB_HTTP_PORT=80 ./k8s/scripts/verify-k8s.sh

NAMESPACE="${NAMESPACE:-poi}"
LB_HTTP_PORT="${LB_HTTP_PORT:-80}"

echo "Recursos Kubernetes no namespace '${NAMESPACE}'"
kubectl -n "$NAMESPACE" get pods,deployments,statefulsets,svc,pvc -o wide

echo ""
echo "Acesso direto aos NodePorts do Minikube"
MINIKUBE_IP="$(minikube ip)"
echo "Minikube IP: ${MINIKUBE_IP}"

for entry in "cpu:30080" "memory:30081" "io:30082"; do
    name="${entry%%:*}"
    port="${entry##*:}"
    echo "--- ${name}-bound (NodePort ${port}) ---"
    curl -fsS "http://${MINIKUBE_IP}:${port}/health" && echo ""
    curl -fsS "http://${MINIKUBE_IP}:${port}/info" && echo ""
done

echo ""
echo "Acesso via Nginx local (127.0.0.1:80)"
echo "--- /health ---"
curl -fsS "http://127.0.0.1/health"
for app in cpu memory io; do
    echo "--- /${app}/health & /${app}/info ---"
    curl -fsS "http://127.0.0.1/${app}/health" && echo ""
    curl -fsS "http://127.0.0.1/${app}/info" && echo ""
done

if [ -n "${LB_VIP:-}" ]; then
    echo ""
    echo "Acesso externo via Load Balancer (${LB_VIP}:${LB_HTTP_PORT})"
    BASE_URL="http://${LB_VIP}:${LB_HTTP_PORT}"
    echo "--- ${BASE_URL}/health ---"
    curl -fsS "${BASE_URL}/health"
    for app in cpu memory io; do
        echo "--- ${BASE_URL}/${app}/health & /${app}/info ---"
        curl -fsS "${BASE_URL}/${app}/health" && echo ""
        curl -fsS "${BASE_URL}/${app}/info" && echo ""
    done
else
    echo ""
    echo "=== [4/4] Acesso via Load Balancer ignorado (defina LB_VIP=<ip> para testar) ==="
fi

echo ""
echo "Verificação concluída com sucesso."
