✅ How to Deploy Open WebUI with Azure AI Foundry (Entra‑Only) via a Proxy
(Azure Container Apps, Managed Identity, PostgreSQL)

This guide shows how to run Open WebUI with an Azure AI Foundry model (e.g. DeepSeek) without API keys, using Managed Identity, in a Microsoft‑managed subscription.


🧠 Architecture (What You’re Building)
Open WebUI (ACA)
   |
   |  OpenAI-compatible HTTP
   |  (API key = local proxy secret)
   v
Proxy (ACA, Managed Identity)
   |
   |  Entra ID token
   v
Azure AI Foundry (DeepSeek)

Why this is required

Foundry has disableLocalAuth=true
Open WebUI cannot acquire Entra tokens
A proxy is the only compliant integration point


✅ Prerequisites

Azure subscription (Microsoft‑managed / locked down is fine)
Azure AI Foundry deployment (e.g. DeepSeek‑V3.2‑Speciale)
Azure PostgreSQL Flexible Server
Azure Container Apps environment
Azure Container Registry (ACR)
GitHub repo for proxy code


1️⃣ Deploy PostgreSQL (Persistence)
Create Azure Database for PostgreSQL – Flexible Server.
Create DB + user:
SQLCREATE DATABASE openwebui;CREATE USER openwebui_user WITH PASSWORD '<strong password>';GRANT ALL PRIVILEGES ON DATABASE openwebui TO openwebui_user;Show more lines
✅ Important

Use postgresql:// (not postgres://)
URL‑encode password characters
Always include sslmode=require

Example:
postgresql://openwebui_user:P%40ss%23word@myserver.postgres.database.azure.com:5432/openwebui?sslmode=require


2️⃣ Deploy Open WebUI (Container App)
✅ Image
DO NOT use :main
Use a pinned release:
ghcr.io/open-webui/open-webui:v0.7.2

✅ Ingress

Enabled
Accept traffic from anywhere
Port: 8080

✅ Environment Variables
DATABASE_URL=<postgres url>

⚠️ Important behavior

If DB is down at startup, Open WebUI may crash
Always ensure Postgres is running before restarts


3️⃣ Build the Proxy (OpenAI‑Compatible)
✅ Proxy Responsibilities

Accept OpenAI‑style requests
Enforce a local API key
Acquire Entra token via Managed Identity
Call Foundry MaaS endpoint
Strip reasoning / disable streaming

✅ Minimal Proxy Logic
Key behaviors you must include:
Python# force non-streaming (prevents hangs)body["stream"] = False# enforce local API keyAuthorization: Bearer EXPECTED_API_KEYShow more lines

4️⃣ Build & Push Proxy Image to ACR
From Cloud Shell:
Shellaz acr build \  --registry <your-acr> \  --image openwebui-proxy:v1 \  https://github.com/<you>/<repo>.gitShow more lines
Repeat with new tags (v2, v3) for patches.

5️⃣ Deploy Proxy (Container App)
✅ Ingress

Enabled
Accept traffic from anywhere
Port: 8000

✅ This avoids ACA internal DNS ambiguity.
✅ Environment Variables
EXPECTED_API_KEY=<shared secret>
FOUNDRY_CHAT_URL=https://<foundry>.services.ai.azure.com/models/chat/completions?api-version=2024-05-01-preview
DEFAULT_MODEL=DeepSeek-V3.2-Speciale
TOKEN_SCOPE=https://cognitiveservices.azure.com/.default


6️⃣ Enable Managed Identity + RBAC (Critical)
✅ Enable System‑Assigned Identity on Proxy
Azure Portal → Proxy Container App → Identity → On

✅ Grant ACR Pull
On ACR:

Role: AcrPull
Principal: proxy managed identity


✅ Grant Foundry Data‑Plane Permission (MOST MISSED STEP)
On Foundry / Cognitive Services account:
Assign ONE of:

✅ Cognitive Services OpenAI User (preferred)
✅ Cognitive Services User

To:

Managed identity → openwebui-proxy


This grants:
Microsoft.CognitiveServices/accounts/MaaS/chat/completions/action


Without this, calls will 403 or hang.

7️⃣ Wire Open WebUI → Proxy
Open WebUI → Admin → Settings → Connections
✅ Connection Settings

Provider: OpenAI
Base URL:

https://<proxy-endpoint-from-portal>/v1


API Key: same as EXPECTED_API_KEY
Model ID:

DeepSeek-V3.2-Speciale

Disable all other connections.

✅ Validation Checklist
✅ Proxy Health
Shellcurl -H "Authorization: Bearer <EXPECTED_API_KEY>" \  https://<proxy-endpoint>/v1/modelsShow more lines
Expected:
JSON{"id":"DeepSeek-V3.2-Speciale"}Show more lines
✅ Open WebUI

Select model
Ask: hello world
Response appears ✅


⚠️ Known Pitfalls (Lessons Learned)

❌ Do not rely on ACA internal DNS
❌ Do not use Open WebUI :main
❌ Do not skip Foundry data‑plane RBAC
❌ Do not allow streaming responses
✅ Always trust portal‑generated endpoints
✅ Restart proxy after RBAC changes


✅ Why This Architecture Is Correct

Entra‑only compliant
No API keys to Foundry
Least privilege
Works in locked‑down subscriptions
Matches Microsoft reference patterns


✅ Final Notes
This setup is hard mode Azure.
If you got this running, you now understand:

ACA ingress behavior
Managed Identity token flow
Foundry MaaS authorization
Open WebUI internals
Why a VM would have been easier 😄
