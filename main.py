import os
import json
import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from azure.identity import DefaultAzureCredential

app = FastAPI()

# ===== Environment variables =====
FOUNDRY_CHAT_URL = os.environ["FOUNDRY_CHAT_URL"]
DEFAULT_MODEL = os.getenv("DEFAULT_MODEL", "DeepSeek-V3.2-Speciale")
EXPECTED_API_KEY = os.getenv("EXPECTED_API_KEY", "")
TOKEN_SCOPE = os.getenv(
    "TOKEN_SCOPE",
    "https://cognitiveservices.azure.com/.default"
)

# ===== Helpers =====
def check_openwebui_key(req: Request):
    if not EXPECTED_API_KEY:
        return
    auth = req.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    supplied = auth.split(" ", 1)[1].strip()
    if supplied != EXPECTED_API_KEY:
        raise HTTPException(status_code=403, detail="Invalid API key")

def get_aad_token() -> str:
    credential = DefaultAzureCredential()
    token = credential.get_token(TOKEN_SCOPE)
    return token.token

# ===== Routes =====
@app.get("/v1/models")
async def list_models(request: Request):
    check_openwebui_key(request)
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL,
                "object": "model"
            }
        ]
    }

@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    check_openwebui_key(request)

    body = await request.json()
    body["model"] = body.get("model") or DEFAULT_MODEL
    body["stream"] = False 

    aad_token = get_aad_token()

    headers = {
        "Authorization": f"Bearer {aad_token}",
        "Content-Type": "application/json",
    }

    response = requests.post(
        FOUNDRY_CHAT_URL,
        headers=headers,
        data=json.dumps(body),
        timeout=30
    )

    if response.status_code >= 400:
        return JSONResponse(
            status_code=response.status_code,
            content={"error": response.text}
        )

    return JSONResponse(
        status_code=response.status_code,
        content=response.json()
    )

@app.get("/healthz")
async def healthz():
    return {"ok": True}
