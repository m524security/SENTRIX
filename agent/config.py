"""
SENTRIX — Central Configuration
Every environment variable the system reads is defined here, once, with a
sane default. Both `agent/` and `api/` import from this module instead of
calling os.getenv() ad-hoc, so the whole system agrees on settings.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str = "") -> str:
    val = os.getenv(name)
    return val if val is not None else default


def _bool(name: str, default: str = "false") -> bool:
    return _env(name, default).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Product identity + runtime mode
# ---------------------------------------------------------------------------
PRODUCT_NAME = "SENTRIX"
VERSION = "4.1.0"
# simulation | live | hybrid  — see agent/ingestion/fleet_source.py
MODE = _env("SENTRIX_MODE", "simulation").strip().lower()
# Shared secret protecting write/ingest endpoints (see agent/security.py)
API_TOKEN = _env("SENTRIX_API_TOKEN", "").strip()

# HMAC key signing the tamper-evident audit trail (agent/memory/audit_logger.py)
# and outgoing action-webhook payloads (agent/actions/executor.py).
# Known insecure defaults are detected so production can refuse to start with them.
_INSECURE_HMAC_DEFAULTS = {
    "", "sentrix-dev-key-change-in-production", "change-me-in-production", "changeme",
}
HMAC_KEY = _env("SENTRIX_HMAC_KEY", "sentrix-dev-key-change-in-production").strip()
HMAC_KEY_IS_DEFAULT = HMAC_KEY.lower() in _INSECURE_HMAC_DEFAULTS


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------
CYCLE_SECONDS = _int("SENTRIX_CYCLE_SECONDS", 60)
API_WS_URL = _env("SENTRIX_API_WS", "ws://localhost:8000/ws")
ENV_NAME = _env("SENTRIX_ENV", "development").strip().lower()
LOG_LEVEL = _env("SENTRIX_LOG_LEVEL", "info")
WORLD_TICK_SECONDS = _int("SENTRIX_WORLD_TICK_SECONDS", 300)  # how often simulated asset state drifts

# ---------------------------------------------------------------------------
# LLM provider — pluggable. Any OpenAI-compatible endpoint works out of the box.
# ---------------------------------------------------------------------------
# Generic override surface. If LLM_PROVIDER is set, it wins. Otherwise SENTRIX
# auto-detects the first provider that has an API key configured, in this
# priority order: openrouter -> nvidia -> gemini -> openai_compatible.
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "").strip().lower()
LLM_API_KEY = os.getenv("LLM_API_KEY", "").strip()
LLM_MODEL = os.getenv("LLM_MODEL", "").strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()

# Egress allow-list. Every reasoning cycle ships asset names, criticality and
# recovery gaps to whatever host LLM_BASE_URL points at — a misconfigured or
# injected base URL is an exfiltration path straight out of the fleet. In
# production, pin the hosts prompts are allowed to reach. Empty = unrestricted
# (fine for dev; enforce_startup_policy warns loudly in production).
LLM_ALLOWED_HOSTS = [h.strip().lower() for h in
                     _env("SENTRIX_LLM_ALLOWED_HOSTS", "").split(",") if h.strip()]


def llm_host_allowed(url: str = None) -> tuple:
    """(allowed, host). No allow-list configured => everything allowed."""
    from urllib.parse import urlparse
    url = url if url is not None else LLM_BASE_URL
    if not url or not LLM_ALLOWED_HOSTS:
        return True, ""
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False, url[:60]
    # exact match or subdomain of an allowed host
    ok = any(host == a or host.endswith("." + a) for a in LLM_ALLOWED_HOSTS)
    return ok, host

# Provider-specific convenience variables (so a user can just paste one key
# without learning the generic LLM_* names).
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "openrouter/free").strip()
OPENROUTER_BASE_URL = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1").strip()

NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY", "").strip()
NVIDIA_MODEL = os.getenv("NVIDIA_MODEL", "meta/llama-3.3-70b-instruct").strip()
NVIDIA_BASE_URL = os.getenv("NVIDIA_BASE_URL", "https://integrate.api.nvidia.com/v1").strip()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.0-flash-lite").strip()

# Truly generic OpenAI-compatible slot — point this at Groq, Together,
# Fireworks, DeepInfra, a self-hosted vLLM/Ollama server, anything that
# speaks the OpenAI chat-completions wire format.
OPENAI_COMPAT_API_KEY = os.getenv("OPENAI_COMPAT_API_KEY", "").strip()
OPENAI_COMPAT_MODEL = os.getenv("OPENAI_COMPAT_MODEL", "").strip()
OPENAI_COMPAT_BASE_URL = os.getenv("OPENAI_COMPAT_BASE_URL", "").strip()

# Optional second provider used purely for resilience: if the primary
# provider errors out mid-cycle, SENTRIX retries once on this one before
# dropping to the deterministic rule engine. Leave unset to disable.
LLM_FALLBACK_PROVIDER = os.getenv("LLM_FALLBACK_PROVIDER", "").strip().lower()
LLM_FALLBACK_API_KEY = os.getenv("LLM_FALLBACK_API_KEY", "").strip()
LLM_FALLBACK_MODEL = os.getenv("LLM_FALLBACK_MODEL", "").strip()
LLM_FALLBACK_BASE_URL = os.getenv("LLM_FALLBACK_BASE_URL", "").strip()

# Local Ollama — tried after the configured cloud provider(s), before the
# rule engine. Free, but only useful if the user actually runs Ollama.
USE_LOCAL_FALLBACK = _bool("SENTRIX_USE_LOCAL", "false")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")

# Locally fine-tuned SLM (LoRA adapter from scripts/slm_train.py). When enabled
# AND an adapter has been trained, it becomes the agent's PRIMARY reasoning
# brain — fully offline, no cloud, no Ollama required. Falls through to the
# normal provider chain / rule engine if the adapter is missing or errors.
USE_SLM = _bool("SENTRIX_USE_SLM", "false")
SLM_MAX_ASSETS = _int("SENTRIX_SLM_MAX_ASSETS", 24)  # CPU budget per cycle
# Safety: when the SLM's action disagrees with SENTRIX's deterministic policy,
# snap to the policy action (keeping the SLM's explanation). Keeps a small/lightly
# trained model safe. Set false to see the SLM's raw, unguarded decisions.
SLM_STRICT = _bool("SENTRIX_SLM_STRICT", "true")

LLM_TIMEOUT_SECONDS = _int("SENTRIX_LLM_TIMEOUT", 30)
LLM_MAX_RETRIES = _int("SENTRIX_LLM_MAX_RETRIES", 1)

# ---------------------------------------------------------------------------
# Persistence / external services
# ---------------------------------------------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
REDIS_URL = os.getenv("REDIS_URL", "").strip()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT = os.getenv("TELEGRAM_CHAT", "").strip()

CORS_ORIGINS = [o.strip() for o in _env("SENTRIX_CORS_ORIGINS", "http://localhost:3000").split(",") if o.strip()]


def resolved_provider_chain() -> list:
    """
    Returns an ordered list of provider configs to try, each a dict with
    provider/api_key/model/base_url. Built from explicit LLM_* overrides
    first, then whichever provider-specific keys are actually populated.
    This is what makes "just paste any API key" work without extra config.
    """
    chain = []

    def add(provider, api_key, model, base_url=""):
        if api_key:
            chain.append({"provider": provider, "api_key": api_key, "model": model, "base_url": base_url})

    # Explicit override always goes first if fully specified.
    if LLM_PROVIDER and LLM_API_KEY:
        add(LLM_PROVIDER, LLM_API_KEY, LLM_MODEL or _default_model_for(LLM_PROVIDER), LLM_BASE_URL)

    # Auto-detected providers, in priority order. Skipped if already added above.
    add("openrouter", OPENROUTER_API_KEY, OPENROUTER_MODEL, OPENROUTER_BASE_URL)
    add("nvidia", NVIDIA_API_KEY, NVIDIA_MODEL, NVIDIA_BASE_URL)
    add("gemini", GEMINI_API_KEY, GEMINI_MODEL)
    add("openai_compatible", OPENAI_COMPAT_API_KEY, OPENAI_COMPAT_MODEL, OPENAI_COMPAT_BASE_URL)

    # De-duplicate by provider name, keeping first occurrence (highest priority).
    seen = set()
    deduped = []
    for c in chain:
        if c["provider"] in seen:
            continue
        seen.add(c["provider"])
        deduped.append(c)

    # Explicit fallback provider, appended last so it's tried only if every
    # primary candidate above fails.
    if LLM_FALLBACK_PROVIDER and LLM_FALLBACK_API_KEY:
        deduped.append({
            "provider": LLM_FALLBACK_PROVIDER,
            "api_key": LLM_FALLBACK_API_KEY,
            "model": LLM_FALLBACK_MODEL or _default_model_for(LLM_FALLBACK_PROVIDER),
            "base_url": LLM_FALLBACK_BASE_URL,
        })

    return deduped


def _default_model_for(provider: str) -> str:
    return {
        "openrouter": OPENROUTER_MODEL,
        "nvidia": NVIDIA_MODEL,
        "gemini": GEMINI_MODEL,
        "openai_compatible": OPENAI_COMPAT_MODEL,
    }.get(provider, "")


def active_provider_names() -> list:
    return [c["provider"] for c in resolved_provider_chain()]


# ---------------------------------------------------------------------------
# v4.1 — Action execution, persistence, agency, notifications, limits
# ---------------------------------------------------------------------------
# off      : actions only logged (v4.0 behavior)
# dry_run  : connectors invoked in no-op mode, full record of WOULD-do
# approve  : actions queued; a human approves via /api/actions/approve
# auto     : actions executed immediately (use with care)
ACTION_MODE = _env("SENTRIX_ACTION_MODE", "dry_run").strip().lower()
ACTION_WEBHOOK_URL = _env("SENTRIX_ACTION_WEBHOOK", "").strip()   # generic action webhook
RETRY_COMMAND = _env("SENTRIX_RETRY_COMMAND", "").strip()          # e.g. "restic backup {asset_id}"
RESTORE_TEST_COMMAND = _env("SENTRIX_RESTORE_TEST_COMMAND", "").strip()

DB_PATH = _env("SENTRIX_DB_PATH", "data/sentrix.db")

# LLM batching + async
BATCH_SIZE = _int("SENTRIX_BATCH_SIZE", 25)          # assets per LLM prompt

# Agentic extras
AGENCY = _bool("SENTRIX_AGENCY", "false")            # tool-use refinement pass
CRITIC = _bool("SENTRIX_CRITIC", "false")            # analyst+critic before P1 pages
RAG = _bool("SENTRIX_RAG", "true")                   # similar-incident recall into prompts

# Notification fan-out (any subset)
SLACK_WEBHOOK_URL = _env("SLACK_WEBHOOK_URL", "").strip()
DISCORD_WEBHOOK_URL = _env("DISCORD_WEBHOOK_URL", "").strip()
PAGERDUTY_ROUTING_KEY = _env("PAGERDUTY_ROUTING_KEY", "").strip()
SMTP_HOST = _env("SMTP_HOST", "").strip()
SMTP_PORT = _int("SMTP_PORT", 587)
SMTP_USER = _env("SMTP_USER", "").strip()
SMTP_PASSWORD = _env("SMTP_PASSWORD", "").strip()
ALERT_EMAIL_TO = _env("SENTRIX_ALERT_EMAIL_TO", "").strip()

RATE_LIMIT_PER_MIN = _int("SENTRIX_RATE_LIMIT_PER_MIN", 20)
