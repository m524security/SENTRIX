# SENTRIX Security Model

This document describes the security architecture of SENTRIX, the controls in
place, and how to deploy it safely. It reflects the fixes applied after a full
code security audit.

## Reporting a vulnerability

Open a private GitHub security advisory on this repository (Security tab →
"Report a vulnerability"). Please do not open public issues for
security-sensitive bugs.

---

## Authentication & authorization

**Write token.** Every endpoint that mutates state or spends resources requires
`Authorization: Bearer <SENTRIX_API_TOKEN>`:

| Endpoint | Protection |
|---|---|
| `POST /api/agent/cycle` | write token |
| `POST /api/ingest` | write token + rate limit (240/min) |
| `POST /api/ingest/reset` | write token |
| `POST /api/actions/approve` / `reject` | write token |
| `POST /api/simulate/trigger` | write token + rate limit (10/min) — runs the LLM chain and broadcasts to all dashboards |
| `POST /api/visual-analysis/upload` | write token + rate limit (12/min) + 5 MB cap + strict base64 + PNG/JPEG magic-byte validation |
| `POST /api/chat` | rate limit (LLM proxy) |

Token comparison uses `hmac.compare_digest` (constant-time) — a plain `!=`
short-circuits on the first differing byte and leaks token prefixes through
response timing.

**Startup policy.** With `SENTRIX_ENV=production` the process **refuses to
start** unless:

- `SENTRIX_API_TOKEN` is set (no accidental open write API), and
- `SENTRIX_HMAC_KEY` is set to a non-default value (a tamper-evident audit
  trail signed with a publicly known key is forgeable, i.e. worthless).

Generate a strong key:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

## Tamper-evident audit trail

Every agent decision and action is HMAC-SHA256 signed with `SENTRIX_HMAC_KEY`
and appended to `data/audit_log.jsonl` (mirrored to Supabase when configured).
`audit_logger.verify_signature` uses constant-time comparison.

## Action webhook signing

Outgoing `SENTRIX_ACTION_WEBHOOK` requests carry:

```
X-Sentrix-Timestamp: <unix seconds>
X-Sentrix-Signature: sha256=<hex hmac>
```

The signature is `HMAC-SHA256(key, timestamp + "." + body)` with
`SENTRIX_HMAC_KEY`. Receivers (Ansible/AWX, n8n, Lambda) should verify it and
reject timestamps older than ~5 minutes to block replay. Reference verifier:

```python
import hashlib, hmac, time

def verify(headers: dict, body: bytes, key: bytes, max_age=300) -> bool:
    ts = headers.get("X-Sentrix-Timestamp", "")
    sig = headers.get("X-Sentrix-Signature", "")
    if not ts.isdigit() or abs(time.time() - int(ts)) > max_age:
        return False
    expected = "sha256=" + hmac.new(key, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)
```

Without this, anything that learns the webhook URL can spoof orchestrator
actions.

## Command execution safety

The action executor can run real commands (e.g. `restic backup {asset_id}`).
Defenses, in layers:

1. **Never `shell=True`** — commands run via `asyncio.create_subprocess_exec`
   after `shlex.split`, with a hard timeout.
2. **Ingestion-boundary allow-list** — `asset_id` from untrusted telemetry must
   match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}`; anything else is dropped at
   `realtime_gateway.ingest`, so no downstream consumer ever sees a hostile id.
3. **Render-time sanitization** — template fields are stripped to
   `[A-Za-z0-9._-]`, truncated to 128 chars, and leading dashes removed, so a
   value can never become a command flag (`--delete`, `-x …`).
4. **Safety modes** — `SENTRIX_ACTION_MODE` defaults to `dry_run`; `approve`
   requires a human via the write-token-protected approve endpoint.

## Upload handling

`/api/visual-analysis/upload` validates before any bytes touch disk or an
image-parsing library (OpenCV/YOLO — historically a rich CVE surface):

- write token required, per-IP rate limit
- encoded and decoded size capped at 5 MB
- strict base64 (`validate=True`)
- PNG/JPEG magic bytes required
- temp file always deleted (`finally`), filename length-capped

## Browser-facing hardening

All API responses carry:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Permissions-Policy: camera=(), microphone=(), geolocation=()`
- `Content-Security-Policy: default-src 'none'; frame-ancestors 'none'`
  (the API serves JSON; this blocks any accidental rendering of LLM-generated
  text as active content)
- `Strict-Transport-Security` in production

CORS defaults to `http://localhost:3000` and **never** falls back to `*`. Set
`SENTRIX_CORS_ORIGINS` to your dashboard origin(s) in production.

## Container hardening

Both images (`Dockerfile.api`, `Dockerfile.agent`):

- multi-stage builds — compilers/pip caches never ship to runtime
- run as non-root user `sentrix` (uid 10001)
- no `tests/` in production images
- `HEALTHCHECK` wired (API: `/api/health`; agent: heartbeat file), so
  `restart: unless-stopped` can actually detect hangs
- LogHub samples fetched at build time (idempotent, degrades gracefully) —
  fresh clones build with one command
- `.env` is injected at runtime, never baked into images

## AI safety boundaries

SENTRIX is an AI-driven platform whose LLM outputs influence operator decisions
and (in `auto` mode) real commands — so **every LLM output is treated as
untrusted input** (`agent/ai_safety.py`):

- **Action allow-list** — LLM-produced assessments may only carry actions from
  the reasoning core's real vocabulary (`NONE`, `WARN`, `MANUAL_REVIEW`,
  `ESCALATE_P1/P2`, `RETRY_BACKUP`, `SCHEDULE_RESTORE_TEST`). Anything the
  model invents is downgraded to `MANUAL_REVIEW` — an unknown verb surfaces to
  a human, never flows toward the executor.
- **Schema validation** — risk scores and confidence are numeric-validated and
  clamped (NaN/inf/absurd values neutralized); explanations are
  control-character-stripped and length-capped.
- **asset_id gate** — assessments whose asset_id fails the same allow-list
  regex enforced at the ingestion boundary are dropped, so a poisoned LLM
  output can't smuggle a hostile id into the approve-action → executor path.
  Validation runs in both the agent loop (before `dispatch_action`) and the
  API's `normalize_assessments` (before persistence/broadcast).
- **Prompt-injection guarding** — chat input is size-capped,
  control-character-stripped, and screened for injection framing ("ignore all
  previous instructions", system-tag smuggling, etc.); suspected attempts are
  logged. The chat LLM has no tools and its output is display-only — that
  architectural containment is the real defense; the heuristics add detection.
- **Output scrubbing** — LLM replies rendered in the dashboard are sanitized
  and length-capped.

## Observability

- `GET /livez` — liveness probe (process + event loop up)
- `GET /readyz` — readiness probe; returns **503** if the fleet source or
  persistence layer can't be read, so orchestrators stop routing to a broken
  replica
- `GET /metrics` — Prometheus text-exposition format (stdlib-only, no client
  library): `sentrix_up`, uptime, asset totals/healthy/critical, cycles,
  websocket clients. Scrape with a standard Prometheus job; pairs directly
  with Grafana.
- Docker `HEALTHCHECK`s in both images (API polls `/api/health`; the agent
  writes a per-cycle heartbeat file).

## Structured logging

`agent/logging_setup.py` replaces scattered `print()` calls:

- development: human-readable single-line output
- production (or `SENTRIX_LOG_JSON=true`): **one JSON object per line** —
  ready for Loki/CloudWatch/ELK with no parsing regexes

Security-relevant events (auth warnings, dropped hostile asset_ids, unknown
LLM actions, suspected prompt injection, audit-write failures) all flow
through it with structured `extra` fields.

## Compose-level hardening

`docker-compose.yml` adds defense-in-depth on top of the Dockerfiles:

- `cap_drop: [ALL]` on every service (`NET_BIND_SERVICE` etc. added back only
  where nginx needs it)
- `security_opt: no-new-privileges:true` — blocks setuid escalation
- `depends_on: condition: service_healthy` — the agent and dashboard wait for
  a *healthy* API, not merely a started container
- named volumes for `/app/data` — the audit trail and SQLite persistence
  survive container recreation

## Supply chain

- `requirements.lock` pins exact versions — use it for CI/production installs:
  `pip install -r requirements.lock`
- Dependabot monitors pip, npm, Docker base images, and GitHub Actions
- CI (`.github/workflows/security.yml`) runs on every push and weekly:
  CodeQL SAST (Python + JS), `pip-audit` CVE checks against the lock file,
  gitleaks secret scanning over full history, and Trivy scans of both images
  (fails on unpatched CRITICAL/HIGH)

## Deployment checklist

```
[ ] SENTRIX_ENV=production
[ ] SENTRIX_API_TOKEN set to a long random value
[ ] SENTRIX_HMAC_KEY set (secrets.token_hex(32)) — startup enforces this
[ ] SENTRIX_CORS_ORIGINS set to the real dashboard origin(s)
[ ] TLS terminated in front of the API (reverse proxy / load balancer)
[ ] SENTRIX_ACTION_MODE left at dry_run or approve until connectors are vetted
[ ] Webhook receivers verify X-Sentrix-Signature
[ ] pip install -r requirements.lock (not requirements.txt) in prod builds
```

## What leaves your network

**Read this before pointing SENTRIX at a real fleet.**

When a cloud LLM provider is configured, every reasoning cycle sends the
following to that third party:

| Data | Sent? | Notes |
|---|---|---|
| Asset names | **Yes** | e.g. `payments-db-prod-01` — reveals infrastructure naming |
| Asset criticality / tier | **Yes** | reveals which systems matter most |
| Backup timings, RPO targets, failure counts | **Yes** | reveals your recovery posture |
| Log evidence lines | **Yes** | excerpts from LogHub-derived telemetry |
| Recovery confidence + gaps | **Yes** | reveals which systems you *cannot* recover |
| Credentials, secrets, backup contents | **No** | never included in prompts |
| Restored bytes / integrity samples | **No** | entropy + checksums computed locally; only the verdict is stored |

That last row of "yes" is the sensitive one: an attacker who obtained your LLM
provider logs would learn exactly which of your critical systems are not
provably recoverable. That is a target list.

**The offline answer — use it if this matters.** SENTRIX ships a fully local
reasoning path and requires no cloud provider:

```bash
SENTRIX_SLM_ENABLED=true      # local fine-tuned Qwen2.5-0.5B LoRA adapter
SENTRIX_LLM_PROVIDER=ollama   # or point at any self-hosted OpenAI-compatible endpoint
SENTRIX_LLM_BASE_URL=http://localhost:11434/v1
```

With either set, no asset data leaves the host. The rule engine (no model at
all) also scores, schedules and alerts on its own — `reason_with_fallback`
degrades all the way down.

**Egress allow-listing.** In production, set `SENTRIX_LLM_ALLOWED_HOSTS` to the
hosts you expect. A misconfigured or injected `SENTRIX_LLM_BASE_URL` is an
exfiltration path — this pins where prompts may go:

```bash
SENTRIX_ENV=production
SENTRIX_LLM_ALLOWED_HOSTS=localhost,127.0.0.1,openrouter.ai
```

Startup refuses to boot if the configured base URL's host isn't on the list.

## Known limitations / future work

- **[FIXED]** The `/ws` feed is now token-gated (`/ws?token=...`, constant-time
  comparison) whenever an API token is configured — always, in production. It
  streams live asset names and criticality, which is fleet intelligence, not
  public data. Dev with no token configured stays open so local work isn't
  blocked. The dashboard supplies the token via `REACT_APP_WS_TOKEN` or a
  `sentrix_token` localStorage key.
- Rate limiting is per-process in-memory; behind multiple replicas, move it to
  Redis.
- Consider mTLS between agent and API for zero-trust deployments.


## Frontend supply chain

The dashboard is built on Create React App, which React deprecated for new apps
in February 2025 and which now receives no security patches. `npm audit` on the
lock file reports real CVEs (ReDoS in `nth-check`, an RCE-class issue in
`serialize-javascript`, CSS-stringify XSS in `postcss`) — **all of them nested
inside the CRA/webpack/jest dev toolchain.**

What this does and doesn't mean:

- **Not exposed at runtime.** `dashboard/Dockerfile` is a multi-stage build; the
  runtime image contains only static HTML/CSS/JS served by nginx. No webpack,
  no jest, no `nth-check` reaches the running container.
- **A build-time and developer-machine risk.** These CVEs are reachable by
  anyone who can influence the build.
- **It will get worse.** No upstream fixes are coming.

CI gates this: `npm audit --omit=dev --audit-level=high` **fails the build** on
production dependency issues, while the dev-toolchain audit is advisory (it
would fail permanently on unfixable CRA transitives, which trains people to
ignore it).

**Planned:** migrate to Vite. This is a real migration (build config, env var
prefix `REACT_APP_` → `VITE_`, entry point, test runner), not a drop-in swap, so
it is tracked as work rather than claimed as done.

## Release integrity

Deliberately noted: the audit trail and evidence ledger are HMAC tamper-evident,
but git history and release tags currently carry no provenance. Sign tags before
any release anyone else depends on:

```bash
git tag -s v4.2.0 -m "SENTRIX v4.2.0"
git verify-tag v4.2.0
```

Large model binaries (`yolov8n.pt`, the LoRA adapter) are tracked via
`.gitattributes` for Git LFS. Run once to rewrite existing history:

```bash
git lfs install
git lfs migrate import --include="*.pt,*.safetensors"
```
