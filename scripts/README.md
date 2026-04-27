# ragpipeline

Phase 1 RAG stack for the homelab.

## Target Layout

- Workload cluster: `192.168.1.176`, namespace `rag`
- Ingress/auth cluster: `192.168.1.230`, namespace `rag`
- Public URL: `https://ragpipeline.duckdns.org`
- Keycloak: `https://goodmanreunion.duckdns.org/keycloak/realms/ragpipeline`
- Vector database: Qdrant
- Generation provider: OpenAI by default, Anthropic optional
- Ollama: defined but kept offline with `replicas: 0`

## What Phase 1 Deploys

- Qdrant
- ingestion API at `/api/ingest`
- RAG API at `/api/query`
- static frontend
- Secrets/ConfigMaps
- NodePort exposure on host `176`
- nginx Ingress on host `230`
- Keycloak OIDC protection through `oauth2-proxy`

## OpenAI Billing Reality Check

Your paid Codex/ChatGPT subscription does not automatically pay for API usage.
OpenAI documents ChatGPT billing and API Platform billing as separate systems, and
ChatGPT Plus says API usage is separate and billed independently. For this project
you need an API Platform organization with billing enabled and an `OPENAI_API_KEY`.

Check:

1. Sign in at `https://platform.openai.com`.
2. Open `https://platform.openai.com/account/billing/overview`.
3. Confirm the organization has Pay-As-You-Go or another API billing plan.
4. Create a project API key and put it into the Kubernetes secret below.

Sources:

- https://help.openai.com/en/articles/9039756-billing-settings-in-chatgpt-vs-platform
- https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus
- https://platform.openai.com/docs/pricing/

## Required Secrets

Create these before deploying workloads:

```bash
kubectl create namespace rag --dry-run=client -o yaml | kubectl apply -f -

kubectl -n rag create secret generic rag-llm-secrets \
  --from-literal=OPENAI_API_KEY='sk-...' \
  --from-literal=ANTHROPIC_API_KEY=''
```

Create this on host `230` after creating a Keycloak client:

```bash
~/.local/bin/kubectl create namespace rag --dry-run=client -o yaml | ~/.local/bin/kubectl apply -f -

~/.local/bin/kubectl -n rag create secret generic rag-oauth2-proxy-secrets \
  --from-literal=OAUTH2_PROXY_CLIENT_ID='ragpipeline' \
  --from-literal=OAUTH2_PROXY_CLIENT_SECRET='paste-client-secret-here' \
  --from-literal=OAUTH2_PROXY_COOKIE_SECRET='paste-32-byte-base64-secret-here'
```

Generate a cookie secret:

```bash
openssl rand -base64 32
```

## Keycloak Client

Create a client in realm `ragpipeline`:

- Client ID: `ragpipeline`
- Client type: OpenID Connect
- Access type: confidential
- Valid redirect URI: `https://ragpipeline.duckdns.org/oauth2/callback`
- Web origin: `https://ragpipeline.duckdns.org`

## Build And Deploy

From this repo on your workstation:

```bash
./scripts/sync-to-176.sh
```

On host `176`:

```bash
cd /home/ron-goodman/Projects/ragpipeline
./scripts/build-images.sh
./scripts/deploy-176.sh
```

On host `230`:

```bash
cd /home/rongoodman/Projects/ragpipeline
./scripts/deploy-230.sh
```

## ArgoCD

ArgoCD runs on host `230` and syncs the RAG workloads to host `176`.
The ArgoCD setup is split into two Applications because one Application can only
sync to one destination:

- `ragpipeline-workloads`: syncs `k8s/176` to host `176`, namespace `rag`
- `ragpipeline-ingress`: syncs `k8s/230` to host `230`, namespace `rag`

The manifests assume the public Git repo will be:

```text
https://github.com/rawhideron/ragpipeline
```

After pushing this repo there, create/update the ArgoCD apps from host `230`:

```bash
cd /home/rongoodman/Projects/ragpipeline
./scripts/deploy-argocd.sh
```

## Verify

```bash
curl -k https://ragpipeline.duckdns.org/health
```

In the browser:

1. Open `https://ragpipeline.duckdns.org`.
2. Log in through Keycloak.
3. Upload a `.txt`, `.md`, or `.pdf` document.
4. Ask a question about the document.
