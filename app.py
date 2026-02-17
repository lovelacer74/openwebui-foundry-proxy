"""
Foundry MaaS Proxy — Entra-only, streaming, CoT-filtered.

Sits between Open WebUI and Azure AI Foundry.
- Authenticates via Managed Identity (Entra ID)
- Strips <think>...</think> blocks from DeepSeek responses
- Supports streaming (SSE) with in-flight filtering
- Config-driven model routing
"""

import os
import re
import json
import time
import logging
import httpx
import yaml
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse, JSONResponse
from azure.identity.aio import ManagedIdentityCredential, DefaultAzureCredential

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("foundry-proxy")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.getenv("PROXY_CONFIG_PATH", "/app/config.yaml")
EXPECTED_API_KEY = os.getenv("EXPECTED_API_KEY", "")
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT", "120"))
USE_MANAGED_IDENTITY = os.getenv("USE_MANAGED_IDENTITY", "true").lower() == "true"
COGNITIVE_SERVICES_SCOPE = "https://cognitiveservices.azure.com/.default"


def load_config() -> dict:
    """Load model configuration from YAML."""
    if not os.path.exists(CONFIG_PATH):
        logger.warning(f"Config file not found at {CONFIG_PATH}, using env fallback")
        return {
            "models": {
                os.getenv("MODEL_ID", "DeepSeek-V3.2-Speciale"): {
                    "endpoint": os.getenv("FOUNDRY_ENDPOINT", ""),
                    "deployment": os.getenv("MODEL_ID", "DeepSeek-V3.2-Speciale"),
                    "strip_think_tags": True,
                    "max_tokens_default": 4096,
                }
            }
        }
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


CONFIG = load_config()
MODELS = CONFIG.get("models", {})

# ---------------------------------------------------------------------------
# Entra credential (module-level, reused across requests)
# ---------------------------------------------------------------------------
_credential = None


async def get_credential():
    global _credential
    if _credential is None:
        if USE_MANAGED_IDENTITY:
            _credential = ManagedIdentityCredential()
        else:
            # Local dev fallback — uses az login, env vars, etc.
            _credential = DefaultAzureCredential()
    return _credential


async def get_entra_token() -> str:
    """Acquire a Bearer token for Cognitive Services."""
    cred = await get_credential()
    token = await cred.get_token(COGNITIVE_SERVICES_SCOPE)
    return token.token


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
async def verify_api_key(request: Request):
    if not EXPECTED_API_KEY:
        raise HTTPException(500, "EXPECTED_API_KEY not configured on proxy")
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(401, "Missing Bearer token")
    if auth[7:] != EXPECTED_API_KEY:
        raise HTTPException(403, "Invalid API key")


# ---------------------------------------------------------------------------
# CoT filtering
# ---------------------------------------------------------------------------
THINK_PATTERN = re.compile(r"<think>.*?</think>", re.DOTALL)


def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks and clean up whitespace."""
    cleaned = THINK_PATTERN.sub("", text)
    # Collapse multiple newlines left behind
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


class StreamingThinkFilter:
    """
    State machine that filters <think>...</think> from an SSE byte stream.

    Buffers content when inside think tags, emits everything else immediately.
    This avoids the user seeing partial reasoning tokens.
    """

    def __init__(self):
        self.inside_think = False
        self.buffer = ""
        self.tag_buffer = ""  # For partial tag detection

    def process_text(self, text: str) -> str:
        """Process a chunk of text, returning only the non-think content."""
        output = []
        i = 0
        while i < len(text):
            if not self.inside_think:
                # Look for opening <think> tag
                if text[i] == "<":
                    # Buffer potential tag
                    self.tag_buffer += text[i]
                    i += 1
                    continue
                elif self.tag_buffer:
                    # We're accumulating a potential tag
                    self.tag_buffer += text[i]
                    if self.tag_buffer == "<think>":
                        self.inside_think = True
                        self.tag_buffer = ""
                    elif not "<think>".startswith(self.tag_buffer):
                        # Not a think tag, flush the buffer
                        output.append(self.tag_buffer)
                        self.tag_buffer = ""
                    i += 1
                    continue
                else:
                    output.append(text[i])
                    i += 1
            else:
                # Inside think block — look for closing </think>
                if text[i] == "<":
                    self.tag_buffer += text[i]
                    i += 1
                    continue
                elif self.tag_buffer:
                    self.tag_buffer += text[i]
                    if self.tag_buffer == "</think>":
                        self.inside_think = False
                        self.tag_buffer = ""
                    elif not "</think>".startswith(self.tag_buffer):
                        # Not the closing tag, discard (we're inside think)
                        self.tag_buffer = ""
                    i += 1
                    continue
                else:
                    # Discard think content
                    i += 1

        return "".join(output)

    def flush(self) -> str:
        """Flush any remaining buffered content."""
        if self.tag_buffer and not self.inside_think:
            result = self.tag_buffer
            self.tag_buffer = ""
            return result
        return ""


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Foundry proxy starting up")
    logger.info(f"Models configured: {list(MODELS.keys())}")
    logger.info(f"Managed Identity: {USE_MANAGED_IDENTITY}")

    # Warm up credential
    try:
        token = await get_entra_token()
        logger.info("Entra token acquired successfully on startup")
    except Exception as e:
        logger.warning(f"Could not acquire token on startup (may work later): {e}")

    yield

    # Cleanup
    global _credential
    if _credential:
        await _credential.close()
        _credential = None
    logger.info("Foundry proxy shut down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Foundry MaaS Proxy", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok", "models": list(MODELS.keys())}


@app.get("/v1/models", dependencies=[Depends(verify_api_key)])
async def list_models():
    """OpenAI-compatible model listing."""
    model_list = [
        {
            "id": model_id,
            "object": "model",
            "created": 0,
            "owned_by": "azure-foundry",
        }
        for model_id in MODELS
    ]
    return {"object": "list", "data": model_list}


@app.post("/v1/chat/completions", dependencies=[Depends(verify_api_key)])
async def chat_completions(request: Request):
    """
    OpenAI-compatible chat completions endpoint.

    Accepts standard OpenAI request body, routes to the correct Foundry
    deployment, handles Entra auth, filters CoT, and streams back.
    """
    body = await request.json()
    model_id = body.get("model", "")
    wants_stream = body.get("stream", False)

    # Resolve model config
    model_cfg = MODELS.get(model_id)
    if not model_cfg:
        raise HTTPException(
            404,
            f"Model '{model_id}' not configured. Available: {list(MODELS.keys())}",
        )

    endpoint = model_cfg["endpoint"]
    deployment = model_cfg.get("deployment", model_id)
    should_filter = model_cfg.get("strip_think_tags", True)
    max_tokens_default = model_cfg.get("max_tokens_default", 4096)

    # Build Foundry request
    foundry_body = {
        "messages": body.get("messages", []),
        "model": deployment,
        "max_tokens": body.get("max_tokens", max_tokens_default),
        "temperature": body.get("temperature", 0.7),
        "top_p": body.get("top_p", 0.95),
        "stream": wants_stream,
    }

    # Pass through optional params if present
    for key in ("stop", "frequency_penalty", "presence_penalty"):
        if key in body:
            foundry_body[key] = body[key]

    # Get Entra token
    try:
        token = await get_entra_token()
    except Exception as e:
        logger.error(f"Failed to acquire Entra token: {e}")
        raise HTTPException(502, f"Entra token acquisition failed: {e}")

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    foundry_url = f"{endpoint.rstrip('/')}/chat/completions"
    logger.info(
        f"Routing to {foundry_url} | model={deployment} | stream={wants_stream}"
    )

    if wants_stream:
        return await _handle_streaming(
            foundry_url, headers, foundry_body, model_id, should_filter
        )
    else:
        return await _handle_non_streaming(
            foundry_url, headers, foundry_body, model_id, should_filter
        )


async def _handle_non_streaming(
    url: str,
    headers: dict,
    body: dict,
    model_id: str,
    should_filter: bool,
) -> JSONResponse:
    """Non-streaming: call Foundry, filter, return."""
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException:
            raise HTTPException(504, "Foundry request timed out")
        except httpx.RequestError as e:
            logger.error(f"Foundry request failed: {e}")
            raise HTTPException(502, f"Foundry request failed: {e}")

    if resp.status_code != 200:
        logger.error(f"Foundry returned {resp.status_code}: {resp.text[:500]}")
        raise HTTPException(resp.status_code, f"Foundry error: {resp.text[:500]}")

    data = resp.json()

    if should_filter:
        for choice in data.get("choices", []):
            msg = choice.get("message", {})
            if "content" in msg and msg["content"]:
                msg["content"] = strip_think_tags(msg["content"])

    return JSONResponse(content=data)


async def _handle_streaming(
    url: str,
    headers: dict,
    body: dict,
    model_id: str,
    should_filter: bool,
) -> StreamingResponse:
    """Streaming: proxy SSE chunks, filtering think tags in-flight."""

    async def generate():
        think_filter = StreamingThinkFilter() if should_filter else None

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            try:
                async with client.stream(
                    "POST", url, json=body, headers=headers
                ) as resp:
                    if resp.status_code != 200:
                        error_body = await resp.aread()
                        logger.error(
                            f"Foundry stream error {resp.status_code}: "
                            f"{error_body[:500]}"
                        )
                        # Send error as SSE
                        error_data = {
                            "error": {
                                "message": f"Foundry returned {resp.status_code}",
                                "type": "upstream_error",
                            }
                        }
                        yield f"data: {json.dumps(error_data)}\n\n"
                        yield "data: [DONE]\n\n"
                        return

                    async for line in resp.aiter_lines():
                        if not line.startswith("data: "):
                            continue

                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            # Flush any remaining buffer
                            if think_filter:
                                remaining = think_filter.flush()
                                if remaining:
                                    done_chunk = _make_sse_chunk(
                                        remaining, model_id
                                    )
                                    yield f"data: {json.dumps(done_chunk)}\n\n"
                            yield "data: [DONE]\n\n"
                            return

                        try:
                            chunk = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        # Extract delta content
                        delta = (
                            chunk.get("choices", [{}])[0]
                            .get("delta", {})
                            .get("content", "")
                        )

                        if not delta:
                            # Forward non-content chunks (role, finish_reason, etc.)
                            yield f"data: {json.dumps(chunk)}\n\n"
                            continue

                        if think_filter:
                            filtered = think_filter.process_text(delta)
                            if filtered:
                                chunk["choices"][0]["delta"]["content"] = filtered
                                yield f"data: {json.dumps(chunk)}\n\n"
                            # If filtered is empty, we swallowed think content — don't yield
                        else:
                            yield f"data: {json.dumps(chunk)}\n\n"

            except httpx.TimeoutException:
                error_data = {
                    "error": {
                        "message": "Foundry request timed out",
                        "type": "timeout",
                    }
                }
                yield f"data: {json.dumps(error_data)}\n\n"
                yield "data: [DONE]\n\n"
            except httpx.RequestError as e:
                logger.error(f"Stream connection error: {e}")
                error_data = {
                    "error": {
                        "message": f"Connection error: {e}",
                        "type": "connection_error",
                    }
                }
                yield f"data: {json.dumps(error_data)}\n\n"
                yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


def _make_sse_chunk(content: str, model_id: str) -> dict:
    """Build a minimal SSE chunk for injected content."""
    return {
        "id": f"proxy-flush-{int(time.time())}",
        "object": "chat.completion.chunk",
        "model": model_id,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": None,
            }
        ],
    }
