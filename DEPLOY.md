# Foundry MaaS Proxy — Deployment Guide

## Architecture

```
Open WebUI (ACA, pinned v0.7.2)
   │
   │  OpenAI-compatible HTTP (streaming SSE)
   │  Auth: Bearer <EXPECTED_API_KEY>
   ▼
Foundry Proxy (ACA, System MI)
   │
   │  Entra ID Bearer token (auto-acquired)
   │  CoT filtering in-flight
   ▼
Azure AI Foundry (DeepSeek MaaS)
```

## What Changed from v1

| Problem | Old Approach | New Approach |
|---|---|---|
| Slow responses | `stream=False` (wait for full response) | Streaming SSE with in-flight filtering |
| CoT leakage | Broken or missing filtering | State-machine parser strips `<think>` tags from stream |
| Single model | Hardcoded env vars | YAML config, add models without code changes |
| Token management | Unknown | `azure-identity` with auto-refresh |
| Error handling | Unknown | Proper timeout handling, SSE error forwarding |

## Prerequisites

1. **Azure Container Registry** (ACR) — existing or new
2. **Azure Container Apps Environment** — existing
3. **Azure Database for PostgreSQL** — existing, with `openwebui` database
4. **Azure AI Foundry** deployment of DeepSeek-V3.2-Speciale
5. **Managed Identity** with:
   - `AcrPull` on your ACR
   - `Cognitive Services OpenAI User` on the Foundry/Cognitive Services account

## Step 1: Build & Push the Proxy Image

```bash
# From the foundry-proxy directory
az acr build \
  --registry <YOUR_ACR_NAME> \
  --image foundry-proxy:v2 \
  --file Dockerfile \
  .
```

## Step 2: Deploy or Update the Proxy Container App

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `EXPECTED_API_KEY` | ✅ | Shared secret between Open WebUI and proxy |
| `FOUNDRY_ENDPOINT` | ✅ | Full MaaS endpoint URL from Foundry portal |
| `USE_MANAGED_IDENTITY` | ✅ | `true` for ACA, `false` for local dev |
| `REQUEST_TIMEOUT` | ❌ | Seconds before timeout (default: 120) |
| `PROXY_CONFIG_PATH` | ❌ | Path to config.yaml (default: `/app/config.yaml`) |

### Create/Update Container App

```bash
az containerapp create \
  --name foundry-proxy \
  --resource-group <RG> \
  --environment <ACA_ENV> \
  --image <ACR_NAME>.azurecr.io/foundry-proxy:v2 \
  --target-port 8000 \
  --ingress external \
  --min-replicas 1 \
  --max-replicas 3 \
  --cpu 0.5 \
  --memory 1.0Gi \
  --registry-server <ACR_NAME>.azurecr.io \
  --env-vars \
    EXPECTED_API_KEY=<YOUR_SECRET_KEY> \
    FOUNDRY_ENDPOINT=<YOUR_FOUNDRY_MAAS_URL> \
    USE_MANAGED_IDENTITY=true \
    REQUEST_TIMEOUT=120

# Enable system-assigned managed identity
az containerapp identity assign \
  --name foundry-proxy \
  --resource-group <RG> \
  --system-assigned
```

### RBAC (the step everyone forgets)

```bash
# Get the proxy's MI principal ID
PROXY_PRINCIPAL=$(az containerapp identity show \
  --name foundry-proxy \
  --resource-group <RG> \
  --query principalId -o tsv)

# ACR pull access
az role assignment create \
  --assignee $PROXY_PRINCIPAL \
  --role AcrPull \
  --scope /subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.ContainerRegistry/registries/<ACR_NAME>

# Foundry data-plane access (THIS IS THE ONE YOU'LL FORGET)
az role assignment create \
  --assignee $PROXY_PRINCIPAL \
  --role "Cognitive Services OpenAI User" \
  --scope /subscriptions/<SUB>/resourceGroups/<RG>/providers/Microsoft.CognitiveServices/accounts/<FOUNDRY_ACCOUNT>
```

> ⚠️ **After assigning RBAC, restart the proxy container app.** MI tokens are cached.

```bash
az containerapp revision restart \
  --name foundry-proxy \
  --resource-group <RG> \
  --revision <LATEST_REVISION>
```

## Step 3: Validate the Proxy

```bash
# Health check (no auth required)
curl https://<PROXY_ENDPOINT>/health

# Model listing
curl -H "Authorization: Bearer <EXPECTED_API_KEY>" \
  https://<PROXY_ENDPOINT>/v1/models

# Chat completion (non-streaming)
curl -X POST \
  -H "Authorization: Bearer <EXPECTED_API_KEY>" \
  -H "Content-Type: application/json" \
  https://<PROXY_ENDPOINT>/v1/chat/completions \
  -d '{
    "model": "DeepSeek-V3.2-Speciale",
    "messages": [{"role": "user", "content": "Say hello in one sentence."}],
    "stream": false
  }'

# Chat completion (streaming — watch for clean output, no <think> tags)
curl -X POST -N \
  -H "Authorization: Bearer <EXPECTED_API_KEY>" \
  -H "Content-Type: application/json" \
  https://<PROXY_ENDPOINT>/v1/chat/completions \
  -d '{
    "model": "DeepSeek-V3.2-Speciale",
    "messages": [{"role": "user", "content": "Explain quantum entanglement simply."}],
    "stream": true
  }'
```

## Step 4: Configure Open WebUI

1. Go to **Admin → Settings → Connections**
2. Add connection:
   - **Provider:** OpenAI
   - **Base URL:** `https://<PROXY_ENDPOINT_FROM_PORTAL>/v1`
   - **API Key:** Same as `EXPECTED_API_KEY`
3. Disable all other connections
4. Select model `DeepSeek-V3.2-Speciale` in chat
5. Test with a prompt

> ⚠️ Do NOT use the "Azure OpenAI" provider type. Use plain "OpenAI".

## Adding More Models Later

Edit `config.yaml`, add a new entry:

```yaml
models:
  DeepSeek-V3.2-Speciale:
    endpoint: "https://existing-endpoint.models.ai.azure.com"
    deployment: "DeepSeek-V3.2-Speciale"
    strip_think_tags: true
    max_tokens_default: 4096

  Phi-4:
    endpoint: "https://new-phi-endpoint.models.ai.azure.com"
    deployment: "Phi-4"
    strip_think_tags: false
    max_tokens_default: 2048
```

Rebuild image, redeploy. The proxy will advertise both models to Open WebUI.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 403 from Foundry | Missing data-plane RBAC | Assign `Cognitive Services OpenAI User`, restart proxy |
| 401 from proxy | Wrong `EXPECTED_API_KEY` | Check Open WebUI connection settings |
| Timeout | `REQUEST_TIMEOUT` too low, or model is slow | Increase timeout, check Foundry health |
| `<think>` tags visible | `strip_think_tags: false` in config | Set to `true` for DeepSeek models |
| Open WebUI can't connect | Using internal DNS instead of portal URL | Use the FQDN from ACA portal |
| Empty responses | CoT filter ate everything | Check model is actually producing non-think content |

## Known Constraints

- ACA internal DNS is unreliable — always use portal-shown FQDN
- Open WebUI `:main` tag is unstable — pin to `v0.7.2` or a tested release
- MI tokens are cached — restart after any RBAC changes
- Foundry has no server-side response filtering — the proxy is your only option
