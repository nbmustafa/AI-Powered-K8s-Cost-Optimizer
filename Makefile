# ─────────────────────────────────────────────────────────────────────────────
# EKS Cost Optimizer — Makefile
# Usage: make <target>
# ─────────────────────────────────────────────────────────────────────────────

.PHONY: help dev dev-api dev-ui build push test test-backend test-frontend \
        lint deploy-staging deploy-production helm-lint clean logs

# ── Config ────────────────────────────────────────────────────────────────────
AWS_REGION      ?= us-east-1
AWS_ACCOUNT_ID  ?= $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null || echo "UNKNOWN")
ECR_REPO        := $(AWS_ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/eks-cost-optimizer
IMAGE_TAG       ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo "dev")
CLUSTER_STAGING ?= staging-eks
CLUSTER_PROD    ?= production-eks
NAMESPACE       := cost-optimizer
HELM_RELEASE    := eks-cost-optimizer

# Colours
GREEN  := \033[0;32m
YELLOW := \033[0;33m
RESET  := \033[0m

help: ## Show this help
	@echo ""
	@echo "  $(GREEN)EKS Cost Optimizer$(RESET)"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  $(YELLOW)%-22s$(RESET) %s\n", $$1, $$2}'
	@echo ""

# ── Local development ─────────────────────────────────────────────────────────

dev: ## Start full stack locally (API + React served together on :8080)
	@echo "$(GREEN)Starting full stack...$(RESET)"
	@test -n "$(ANTHROPIC_API_KEY)" || (echo "Error: export ANTHROPIC_API_KEY=sk-ant-..." && exit 1)
	docker compose up --build

dev-api: ## Start only the backend API (for frontend hot-reload mode)
	@test -n "$(ANTHROPIC_API_KEY)" || (echo "Error: export ANTHROPIC_API_KEY=sk-ant-..." && exit 1)
	docker compose up backend prometheus

dev-ui: ## Start CRA dev server on :3000 (hot-reload, proxies /api/* to :8080)
	@echo "$(GREEN)Starting CRA dev server on :3000 — ensure 'make dev-api' is running$(RESET)"
	cd frontend && npm start

logs: ## Tail backend container logs
	docker compose logs -f backend

# ── Testing ───────────────────────────────────────────────────────────────────

test: test-backend test-frontend ## Run all tests

test-backend: ## Run Python unit tests
	@echo "$(GREEN)Running backend tests...$(RESET)"
	cd backend && \
		ANTHROPIC_API_KEY=sk-ant-test K8S_IN_CLUSTER=false AWS_DEFAULT_REGION=us-east-1 \
		python -m pytest tests/ -v --tb=short

test-frontend: ## Build React (validates JSX compiles)
	@echo "$(GREEN)Building frontend (compile check)...$(RESET)"
	cd frontend && npm ci --silent && npm run build

lint: ## Lint Python code with ruff
	@echo "$(GREEN)Linting backend...$(RESET)"
	cd backend && pip install ruff -q && ruff check app/

# ── Container image ───────────────────────────────────────────────────────────

build: ## Build the container image (context = repo root)
	@echo "$(GREEN)Building image $(ECR_REPO):$(IMAGE_TAG)$(RESET)"
	docker build \
		-f backend/Dockerfile \
		-t $(ECR_REPO):$(IMAGE_TAG) \
		-t $(ECR_REPO):latest \
		.

push: build ## Build and push to ECR
	@echo "$(GREEN)Pushing to ECR...$(RESET)"
	aws ecr get-login-password --region $(AWS_REGION) \
		| docker login --username AWS --password-stdin $(ECR_REPO)
	docker push $(ECR_REPO):$(IMAGE_TAG)
	docker push $(ECR_REPO):latest
	@echo "$(GREEN)Pushed: $(ECR_REPO):$(IMAGE_TAG)$(RESET)"

ecr-create: ## Create ECR repository (one-time setup)
	aws ecr create-repository \
		--repository-name eks-cost-optimizer \
		--region $(AWS_REGION) \
		--image-scanning-configuration scanOnPush=true

# ── Helm ──────────────────────────────────────────────────────────────────────

helm-lint: ## Lint Helm chart
	@echo "$(GREEN)Linting Helm chart...$(RESET)"
	helm lint ./helm/eks-cost-optimizer \
		-f helm/eks-cost-optimizer/values.yaml \
		-f helm/eks-cost-optimizer/values-staging.yaml
	helm lint ./helm/eks-cost-optimizer \
		-f helm/eks-cost-optimizer/values.yaml \
		-f helm/eks-cost-optimizer/values-production.yaml

helm-template: ## Render Helm templates locally (dry-run)
	helm template $(HELM_RELEASE) ./helm/eks-cost-optimizer \
		-f helm/eks-cost-optimizer/values.yaml \
		-f helm/eks-cost-optimizer/values-staging.yaml \
		--namespace $(NAMESPACE)

deploy-staging: helm-lint ## Deploy to staging cluster
	@echo "$(GREEN)Deploying to staging...$(RESET)"
	aws eks update-kubeconfig --name $(CLUSTER_STAGING) --region $(AWS_REGION)
	kubectl apply -f k8s/rbac.yaml
	helm upgrade --install $(HELM_RELEASE) ./helm/eks-cost-optimizer \
		--namespace $(NAMESPACE) \
		--create-namespace \
		--values helm/eks-cost-optimizer/values.yaml \
		--values helm/eks-cost-optimizer/values-staging.yaml \
		--set image.repository=$(ECR_REPO) \
		--set image.tag=$(IMAGE_TAG) \
		--atomic --timeout 5m --wait
	@echo "$(GREEN)Staging deploy complete$(RESET)"

deploy-production: helm-lint ## Deploy to production cluster (requires IMAGE_TAG)
	@test -n "$(IMAGE_TAG)" || (echo "Error: set IMAGE_TAG=<sha>" && exit 1)
	@echo "$(YELLOW)Deploying $(IMAGE_TAG) to PRODUCTION...$(RESET)"
	@read -p "Are you sure? [y/N] " yn && [ "$$yn" = "y" ]
	aws eks update-kubeconfig --name $(CLUSTER_PROD) --region $(AWS_REGION)
	kubectl apply -f k8s/rbac.yaml
	helm upgrade --install $(HELM_RELEASE) ./helm/eks-cost-optimizer \
		--namespace $(NAMESPACE) \
		--create-namespace \
		--values helm/eks-cost-optimizer/values.yaml \
		--values helm/eks-cost-optimizer/values-production.yaml \
		--set image.repository=$(ECR_REPO) \
		--set image.tag=$(IMAGE_TAG) \
		--atomic --timeout 8m --wait --history-max 5
	@echo "$(GREEN)Production deploy complete$(RESET)"

rollback: ## Roll back last Helm release (usage: make rollback ENV=staging|production)
	@echo "$(YELLOW)Rolling back $(HELM_RELEASE) in $(ENV)...$(RESET)"
	helm rollback $(HELM_RELEASE) -n $(NAMESPACE) --wait

# ── K8s helpers ───────────────────────────────────────────────────────────────

status: ## Show pod status in the cost-optimizer namespace
	kubectl get pods,svc,ingress -n $(NAMESPACE)

shell: ## Open a shell in the running backend pod
	kubectl exec -it -n $(NAMESPACE) \
		$$(kubectl get pod -n $(NAMESPACE) -l app.kubernetes.io/name=eks-cost-optimizer \
			-o jsonpath='{.items[0].metadata.name}') -- /bin/sh

api-test: ## Quick API smoke test against the running pod
	@POD=$$(kubectl get pod -n $(NAMESPACE) -l app.kubernetes.io/name=eks-cost-optimizer \
		-o jsonpath='{.items[0].metadata.name}'); \
	echo "Pod: $$POD"; \
	kubectl exec -n $(NAMESPACE) $$POD -- \
		python -c " \
import urllib.request, json; \
r = urllib.request.urlopen('http://localhost:8080/healthz'); \
print('/healthz:', json.loads(r.read())); \
r2 = urllib.request.urlopen('http://localhost:8080/'); \
body = r2.read().decode(); \
print('SPA served:', '<div id=\"root\">' in body); \
"

port-forward: ## Port-forward the service to localhost:8080
	kubectl port-forward -n $(NAMESPACE) svc/$(HELM_RELEASE) 8080:80

# ── Housekeeping ──────────────────────────────────────────────────────────────

clean: ## Remove local build artifacts
	rm -rf frontend/build frontend/node_modules
	find backend -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find backend -name "*.pyc" -delete 2>/dev/null || true
	docker compose down -v 2>/dev/null || true
