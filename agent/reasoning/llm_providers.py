"""
SENTRIX — Multi-Provider LLM Client
-----------------------------------
SENTRIX's reasoning core should not care which company is behind the model.
This module gives it one function — `call_llm(prompt)` — that tries every
configured provider in priority order and returns the first usable response.

Supported out of the box:
  - OpenRouter        (OpenAI-compatible, hundreds of models, free tier)
  - NVIDIA NIM         (OpenAI-compatible, build.nvidia.com, free tier)
  - Google Gemini      (native google-genai SDK, structured JSON output)
  - Any OpenAI-compatible endpoint (Groq, Together, Fireworks, DeepInfra,
    a self-hosted vLLM/Ollama server — anything that speaks the OpenAI
    chat-completions wire format) via OPENAI_COMPAT_* env vars.
  - Local Ollama        (last resort before the deterministic rule engine)

Adding a new provider that already speaks the OpenAI format requires no
code changes at all — just set OPENAI_COMPAT_BASE_URL / _API_KEY / _MODEL,
or LLM_PROVIDER=openai_compatible with LLM_BASE_URL / LLM_API_KEY / LLM_MODEL.
"""

import json
import re
import time

from agent import config

# Tracks the provider that most recently answered successfully, so the API
# can report "what is actually answering requests right now" rather than
# just "what's configured".
_last_success = {"provider": None, "model": None, "timestamp": None}


class LLMProviderError(Exception):
    """Raised by a single provider attempt. Caught internally — callers of
    call_llm() only ever see LLMAllProvidersFailedError or a clean result."""


class LLMAllProvidersFailedError(Exception):
    def __init__(self, attempts):
        self.attempts = attempts
        summary = "; ".join(f"{a['provider']}: {a['error']}" for a in attempts)
        super().__init__(summary or "No LLM provider is configured")


def last_successful_provider() -> dict:
    return dict(_last_success)


def extract_json(raw: str) -> dict:
    """
    Best-effort extraction of a JSON object from an LLM response, even if
    the model wrapped it in markdown fences or added a stray sentence
    before/after — open-weight free-tier models are not always as obedient
    as top-tier ones about "respond with JSON only".
    """
    if raw is None:
        raise ValueError("Empty response from model")
    text = raw.strip()

    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"```$", "", text.strip())
        text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        return json.loads(candidate)

    raise ValueError(f"Could not find valid JSON in model response: {text[:200]}")


def _call_gemini(cfg: dict, prompt: str) -> str:
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise LLMProviderError(f"google-genai package not installed ({e})")

    client = genai.Client(api_key=cfg["api_key"])
    gen_config = {"response_mime_type": "application/json"}
    try:
        response = client.models.generate_content(
            model=cfg["model"],
            contents=prompt,
            config=types.GenerateContentConfig(**gen_config),
        )
    except TypeError:
        # Older SDK without GenerateContentConfig support — plain call.
        response = client.models.generate_content(model=cfg["model"], contents=prompt)

    text = getattr(response, "text", None)
    if not text:
        raise LLMProviderError("Gemini returned an empty response (possibly blocked by safety filters)")
    return text


def _call_openai_compatible(cfg: dict, prompt: str) -> str:
    try:
        from openai import OpenAI
    except ImportError as e:
        raise LLMProviderError(f"openai package not installed ({e})")

    base_url = cfg.get("base_url") or None
    client = OpenAI(api_key=cfg["api_key"], base_url=base_url, timeout=config.LLM_TIMEOUT_SECONDS)

    kwargs = dict(
        model=cfg["model"],
        messages=[
            {"role": "system", "content": "You always respond with a single valid JSON object and nothing else."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.2,
    )
    try:
        completion = client.chat.completions.create(response_format={"type": "json_object"}, **kwargs)
    except Exception:
        # Some free/open-weight models on OpenRouter or NIM reject
        # response_format — retry once without it before giving up.
        completion = client.chat.completions.create(**kwargs)

    choice = completion.choices[0]
    text = choice.message.content
    if not text:
        raise LLMProviderError("Provider returned an empty completion")
    return text


def _call_ollama(prompt: str) -> str:
    import httpx

    response = httpx.post(
        f"{config.OLLAMA_URL}/api/generate",
        json={"model": config.OLLAMA_MODEL, "prompt": prompt, "stream": False, "format": "json"},
        timeout=config.LLM_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    text = response.json().get("response", "")
    if not text:
        raise LLMProviderError("Ollama returned an empty response")
    return text


_DISPATCH = {
    "gemini": _call_gemini,
    "openrouter": _call_openai_compatible,
    "nvidia": _call_openai_compatible,
    "openai_compatible": _call_openai_compatible,
}


def _attempt_provider(cfg: dict, prompt: str) -> str:
    fn = _DISPATCH.get(cfg["provider"])
    if fn is None:
        raise LLMProviderError(f"Unknown provider '{cfg['provider']}'")
    if not cfg.get("model"):
        raise LLMProviderError("No model configured for this provider")

    last_err = None
    attempts = max(1, config.LLM_MAX_RETRIES)
    for attempt in range(attempts):
        try:
            return fn(cfg, prompt)
        except Exception as e:
            last_err = e
            if attempt < attempts - 1:
                time.sleep(min(2 ** attempt, 4))
    raise LLMProviderError(str(last_err))


def call_llm(prompt: str) -> dict:
    """
    Tries every configured provider, in priority order, and returns
        {"text": <raw model output>, "provider": "openrouter", "model": "..."}
    from the first one that succeeds.

    Raises LLMAllProvidersFailedError if every candidate fails (including
    the case where nothing at all is configured) — callers should catch
    this and drop to the rule engine, never let it crash the agent loop.
    """
    chain = config.resolved_provider_chain()
    attempts = []

    for cfg in chain:
        try:
            text = _attempt_provider(cfg, prompt)
            _last_success["provider"] = cfg["provider"]
            _last_success["model"] = cfg["model"]
            from datetime import datetime
            _last_success["timestamp"] = datetime.utcnow().isoformat()
            return {"text": text, "provider": cfg["provider"], "model": cfg["model"]}
        except Exception as e:
            attempts.append({"provider": cfg["provider"], "model": cfg.get("model", ""), "error": str(e)[:300]})

    if config.USE_LOCAL_FALLBACK:
        try:
            text = _call_ollama(prompt)
            _last_success["provider"] = "ollama"
            _last_success["model"] = config.OLLAMA_MODEL
            return {"text": text, "provider": "ollama", "model": config.OLLAMA_MODEL}
        except Exception as e:
            attempts.append({"provider": "ollama", "model": config.OLLAMA_MODEL, "error": str(e)[:300]})

    raise LLMAllProvidersFailedError(attempts)


def call_llm_json(prompt: str) -> dict:
    """Convenience wrapper: call_llm() + extract_json() in one step.
    Returns a dict with the parsed JSON plus '_provider' and '_model' keys
    describing which backend actually produced it."""
    result = call_llm(prompt)
    parsed = extract_json(result["text"])
    parsed["_provider"] = result["provider"]
    parsed["_model"] = result["model"]
    return parsed


def provider_status() -> dict:
    """Configuration + last-used status, for the dashboard's provider badge."""
    chain = config.resolved_provider_chain()
    return {
        "configured_chain": [c["provider"] for c in chain],
        "configured_details": [{"provider": c["provider"], "model": c["model"]} for c in chain],
        "local_fallback_enabled": config.USE_LOCAL_FALLBACK,
        "last_successful": last_successful_provider(),
    }
