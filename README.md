# EKS Cost Optimizer — Production Deployment Guide

> **AI-powered Kubernetes cost optimization engine** — analyzes nodes, pods, resource utilization and Autopilot metrics to deliver right-sizing recommendations with Claude-generated action plans.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                    EKS Cost Optimizer Stack                      │
│                                                                  │
│  ┌──────────────┐    ┌──────────────────────────────────────┐   │
│  │   React UI   │◄───│         FastAPI Backend               │   │
│  │  Dashboard   │    │  /api/v1/snapshot                     │   │
│  │  + AI Chat   │    │  /api/v1/cost-report                  │   │
│  └──────────────┘    │  /api/v1/ai/analyze (streaming)       │   │
│                      │  /api/v1/ai/chat                       │   │
│                      │  /api/v1/ai/commands                   │   │
│                      └────────────┬─────────────────────────-┘   │
│                                   │                              │
│          ┌────────────────────────┼─────────────────────────┐   │
│          │                        │                          │   │
│   ┌──────▼──────┐    ┌────────────▼────┐    ┌────────────┐  │   │
│   │ Kubernetes  │    │   Prometheus    │    │  Anthropic │  │   │
│   │  API Server │    │  (PromQL p95)   │    │  Claude AI │  │   │
│   │ (nodes/pods)│    │  metrics-server │    │  (advisor) │  │   │
│   └─────────────┘    └─────────────────┘    └────────────┘  │   │
│                                                              │   │
│          ┌───────────────────────────────────────────────┐  │   │
│          │              AWS Services                      │  │   │
│          │  EC2 Pricing API · Cost Explorer · EKS API    │  │   │
│          └───────────────────────────────────────────────┘  │   │
└─────────────────────────────────────────────────────────────────┘
```

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| `kubectl` | ≥ 1.28 | Cluster management |
| `helm` | ≥ 3.14 | Chart deployment |
| `eksctl` | ≥ 0.185 | EKS cluster management |
| `aws-cli` | ≥ 2.15 | AWS resource management |
| `docker` | ≥ 24.0 | Container builds |
| Python | ≥ 3.12 | Backend runtime |

---

## Step 1: AWS Infrastructure Setup

### 1.1 Create ECR Repository

```bash
export AWS_ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
export AWS_REGION=us-east-1
export ECR_REPO="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/eks-cost-optimizer"

aws ecr create-repository \
  --repository-name eks-cost-optimizer \
  --region ${AWS_REGION} \
  --image-scanning-configuration scanOnPush=true

echo "ECR Repo: ${ECR_REPO}"
```

### 1.2 Create IAM Policy and IRSA Role

```bash
export CLUSTER_NAME="production-eks"
export NAMESPACE="cost-optimizer"
export SA_NAME="eks-cost-optimizer"
export POLICY_NAME="EKSCostOptimizerPolicy"

# Create IAM policy
aws iam create-policy \
  --policy-name ${POLICY_NAME} \
  --policy-document file://k8s/iam-policy.json

export POLICY_ARN="arn:aws:iam::${AWS_ACCOUNT_ID}:policy/${POLICY_NAME}"

# Create IRSA role with eksctl
eksctl create iamserviceaccount \
  --cluster=${CLUSTER_NAME} \
  --namespace=${NAMESPACE} \
  --name=${SA_NAME} \
  --attach-policy-arn=${POLICY_ARN} \
  --approve \
  --override-existing-serviceaccounts

echo "IRSA Role ARN: $(aws iam get-role --role-name eksctl-${CLUSTER_NAME}-addon-iamserviceaccount-${NAMESPACE}-${SA_NAME}-Role1 --query Role.Arn --output text)"
```

### 1.3 Store Anthropic API Key in AWS Secrets Manager

```bash
aws secretsmanager create-secret \
  --name eks-cost-optimizer/anthropic-api-key \
  --description "Anthropic Claude API key for EKS Cost Optimizer" \
  --secret-string '{"ANTHROPIC_API_KEY":"sk-ant-YOUR-KEY-HERE"}'
```

---

## Step 2: Build & Push Container Image

```bash
cd backend/

# Authenticate ECR
aws ecr get-login-password --region ${AWS_REGION} | \
  docker login --username AWS --password-stdin ${ECR_REPO}

# Build
docker build -t eks-cost-optimizer:latest .

# Tag & Push
docker tag eks-cost-optimizer:latest ${ECR_REPO}:latest
docker tag eks-cost-optimizer:latest ${ECR_REPO}:$(git rev-parse --short HEAD)

docker push ${ECR_REPO}:latest
docker push ${ECR_REPO}:$(git rev-parse --short HEAD)

echo "Image pushed: ${ECR_REPO}:latest"
```

---

## Step 3: Deploy RBAC

```bash
# Create namespace
kubectl create namespace ${NAMESPACE} --dry-run=client -o yaml | kubectl apply -f -

# Apply RBAC
sed "s/cost-optimizer/${NAMESPACE}/g" k8s/rbac.yaml | kubectl apply -f -

# Verify
kubectl auth can-i list nodes --as=system:serviceaccount:${NAMESPACE}:${SA_NAME}
kubectl auth can-i list pods --as=system:serviceaccount:${NAMESPACE}:${SA_NAME} --all-namespaces
```

---

## Step 4: Deploy via Helm

### 4.1 Create production values override

```bash
cat > helm/eks-cost-optimizer/values-production.yaml << EOF
image:
  repository: ${ECR_REPO}
  tag: "latest"

serviceAccount:
  annotations:
    eks.amazonaws.com/role-arn: "arn:aws:iam::${AWS_ACCOUNT_ID}:role/eksctl-${CLUSTER_NAME}-addon-iamserviceaccount-${NAMESPACE}-${SA_NAME}-Role1"

config:
  CLUSTER_NAME: "${CLUSTER_NAME}"
  AWS_DEFAULT_REGION: "${AWS_REGION}"

ingress:
  hosts:
    - host: eks-cost-optimizer.internal.your-company.com
      paths:
        - path: /
          pathType: Prefix
  annotations:
    alb.ingress.kubernetes.io/certificate-arn: "arn:aws:acm:${AWS_REGION}:${AWS_ACCOUNT_ID}:certificate/YOUR-CERT-ID"
EOF
```

### 4.2 Install Chart

```bash
helm upgrade --install eks-cost-optimizer \
  ./helm/eks-cost-optimizer \
  --namespace ${NAMESPACE} \
  --create-namespace \
  --values helm/eks-cost-optimizer/values.yaml \
  --values helm/eks-cost-optimizer/values-production.yaml \
  --wait \
  --timeout 5m

# Verify pods
kubectl get pods -n ${NAMESPACE}
kubectl logs -n ${NAMESPACE} -l app.kubernetes.io/name=eks-cost-optimizer --tail=50
```

---

## Step 5: Install External Secrets Operator (for API key injection)

```bash
helm repo add external-secrets https://charts.external-secrets.io
helm repo update

helm upgrade --install external-secrets \
  external-secrets/external-secrets \
  --namespace external-secrets \
  --create-namespace

# Create ClusterSecretStore for AWS Secrets Manager
cat <<EOF | kubectl apply -f -
apiVersion: external-secrets.io/v1beta1
kind: ClusterSecretStore
metadata:
  name: aws-secrets-manager
spec:
  provider:
    aws:
      service: SecretsManager
      region: ${AWS_REGION}
      auth:
        jwt:
          serviceAccountRef:
            name: external-secrets
            namespace: external-secrets
EOF
```

---

## Step 6: Configure Prometheus (if not installed)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts

helm upgrade --install kube-prometheus-stack \
  prometheus-community/kube-prometheus-stack \
  --namespace monitoring \
  --create-namespace \
  --set prometheus.prometheusSpec.retention=30d \
  --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.storageClassName=gp3 \
  --set prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage=100Gi
```

---

## Step 7: Local Development

### 7.1 Run backend locally against your cluster

```bash
cd backend/

# Setup Python environment
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure env
cat > .env << EOF
K8S_IN_CLUSTER=false
KUBECONFIG=${HOME}/.kube/config
CLUSTER_NAME=your-cluster-name
AWS_DEFAULT_REGION=us-east-1
ANTHROPIC_API_KEY=sk-ant-your-key
PROMETHEUS_URL=http://localhost:9090
CACHE_TTL_SECONDS=60
LOG_LEVEL=DEBUG
EOF

# Port-forward Prometheus (optional)
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090 &

# Run the backend
uvicorn app.main:app --reload --port 8080

# API docs at: http://localhost:8080/docs
```

### 7.2 Run the UI locally

```bash
cd frontend/
npm install
REACT_APP_API_URL=http://localhost:8080 npm start
```

---

## API Reference

| Endpoint | Method | Description |
|----------|--------|-------------|
| `GET /api/v1/snapshot` | GET | Raw K8s resource snapshot |
| `GET /api/v1/cost-report` | GET | Full cost analysis + savings |
| `GET /api/v1/ai/analyze?focus=quick_wins` | GET | AI analysis (focus: quick_wins, nodes, pods, risk) |
| `GET /api/v1/ai/stream` | GET | Streaming SSE AI analysis |
| `POST /api/v1/ai/chat` | POST | Q&A about your cluster |
| `POST /api/v1/ai/commands` | POST | Generate kubectl/eksctl commands |
| `POST /api/v1/ai/autoscaler-config?tool=karpenter` | POST | Generate Karpenter/HPA/VPA configs |
| `GET /api/v1/nodes` | GET | Node list with metrics |
| `GET /api/v1/pods?namespace=default` | GET | Pod list with resource data |
| `GET /healthz` | GET | Liveness probe |
| `GET /readyz` | GET | Readiness probe |

---

## Monitoring & Alerting

### Prometheus Alerts for the optimizer itself

```yaml
# k8s/prometheus-rules.yaml
apiVersion: monitoring.coreos.com/v1
kind: PrometheusRule
metadata:
  name: eks-cost-optimizer
  namespace: monitoring
spec:
  groups:
    - name: eks-cost-optimizer
      rules:
        - alert: HighClusterCostWaste
          expr: eks_cost_optimizer_waste_percentage > 30
          for: 1h
          labels:
            severity: warning
          annotations:
            summary: "Cluster cost waste above 30%"
```

---

## CI/CD Pipeline (GitHub Actions)

```yaml
# .github/workflows/deploy.yml
name: Deploy EKS Cost Optimizer

on:
  push:
    branches: [main]

jobs:
  build-deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_DEPLOY_ROLE_ARN }}
          aws-region: us-east-1

      - name: Build and push to ECR
        run: |
          aws ecr get-login-password | docker login --username AWS --password-stdin $ECR_REGISTRY
          docker build -t $ECR_REGISTRY/eks-cost-optimizer:${{ github.sha }} ./backend
          docker push $ECR_REGISTRY/eks-cost-optimizer:${{ github.sha }}

      - name: Deploy via Helm
        run: |
          aws eks update-kubeconfig --name production-eks
          helm upgrade --install eks-cost-optimizer ./helm/eks-cost-optimizer \
            --namespace cost-optimizer \
            --set image.tag=${{ github.sha }} \
            --wait
```

---

## Troubleshooting

```bash
# Check pod logs
kubectl logs -n cost-optimizer -l app=eks-cost-optimizer -f

# Check IRSA permissions
kubectl exec -n cost-optimizer deploy/eks-cost-optimizer -- \
  aws sts get-caller-identity

# Test Kubernetes API access
kubectl exec -n cost-optimizer deploy/eks-cost-optimizer -- \
  python -c "from kubernetes import client, config; config.load_incluster_config(); print(client.CoreV1Api().list_node().items[0].metadata.name)"

# Manually trigger a report refresh
kubectl exec -n cost-optimizer deploy/eks-cost-optimizer -- \
  curl -s http://localhost:8080/api/v1/cost-report?force_refresh=true | python -m json.tool

# Test Prometheus connectivity
kubectl exec -n cost-optimizer deploy/eks-cost-optimizer -- \
  curl -s "${PROMETHEUS_URL}/api/v1/query?query=up" | python -m json.tool
```

---

## Security Considerations

1. **Read-only RBAC** — The service account has `get/list/watch` only. No write access.
2. **IRSA** — No long-lived AWS credentials. Pod identity via IAM Roles for Service Accounts.
3. **Non-root container** — Runs as UID 1001, `readOnlyRootFilesystem: true`.
4. **Secrets via External Secrets** — No secrets in Helm values or ConfigMaps.
5. **Internal ALB only** — Ingress uses `alb.ingress.kubernetes.io/scheme: internal`.
6. **Network Policy** — Apply a NetworkPolicy to restrict egress to Prometheus, K8s API, and Anthropic only.

---

## Cost of the Tool Itself

Running this optimizer on EKS:
- **2× m5.large pods**: ~$4.80/month
- **ALB**: ~$16/month
- **Claude API calls**: ~$2–10/month depending on usage

**Expected ROI**: Recovers its own cost within minutes of first run.
