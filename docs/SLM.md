# SENTRIX Local SLM — Fine-Tuned Reasoning Brain

SENTRIX can run its recovery-risk reasoning on a **locally fine-tuned Small
Language Model (SLM)** with no cloud dependency. Two runtimes are supported and
can be combined:

1. **LoRA fine-tune** (`peft` + `transformers`) — a model specialised on
   SENTRIX's own decision policy. Runs through `agent/reasoning/slm_local.py`.
2. **Ollama** — serve a ready-made or exported model locally; SENTRIX already
   speaks to it via `agent/reasoning/llm_providers.py`.

Everything below runs on **CPU**. No GPU required.

---

## 1. Fine-tune the SLM on SENTRIX data

The training data is generated from SENTRIX's own deterministic policy
(`reasoning_core.compute_risk` + `decide_action`), so the SLM learns the exact
risk-scoring rules and JSON output format — no cloud labelling needed.

```bat
:: 1. Build the dataset  (data/slm/train.jsonl, val.jsonl)
venv\Scripts\python scripts\slm_dataset.py --n 4000

:: 2. LoRA fine-tune  ->  models/sentrix-slm-lora/
venv\Scripts\python scripts\slm_train.py --max-steps 200

:: 3. Sanity-check the trained adapter vs ground truth
venv\Scripts\python scripts\slm_infer.py
:: compare against the un-finetuned baseline:
venv\Scripts\python scripts\slm_infer.py --base-only
```

Scale up once the loop works:

```bat
venv\Scripts\python scripts\slm_dataset.py --n 12000
venv\Scripts\python scripts\slm_train.py --base Qwen/Qwen2.5-1.5B-Instruct --max-steps 0 --epochs 2
```

### Make the SLM the agent's brain

Set in `.env`:

```ini
SENTRIX_USE_SLM=true
SENTRIX_SLM_MAX_ASSETS=24      # CPU budget: assets SLM-reasoned per cycle
```

Now `agent/main.py` and the API reason through the fine-tuned model
(`provider: "slm_local"`), fully offline. Every generated decision is still
validated against the deterministic risk math, so an out-of-policy action can
never reach the dashboard. Check it live at `GET /api/ml-status` → `slm_local`.

---

## 2. Run a local model through Ollama

```bat
:: install Ollama (winget) then:
ollama pull qwen2.5:1.5b
```

Point SENTRIX at it as the **primary** provider in `.env`:

```ini
LLM_PROVIDER=openai_compatible
LLM_API_KEY=ollama
LLM_MODEL=qwen2.5:1.5b
LLM_BASE_URL=http://localhost:11434/v1
```

…or as a **fallback** after your cloud provider:

```ini
SENTRIX_USE_LOCAL=true
OLLAMA_MODEL=qwen2.5:1.5b
```

---

## 3. (Advanced) Serve the *fine-tuned* SLM through Ollama

To unify both paths — run your SENTRIX-specialised LoRA model inside Ollama —
merge the adapter, convert to GGUF (llama.cpp), and register it:

```bat
:: merge LoRA into the base and export GGUF (needs llama.cpp convert tooling)
:: then:
ollama create sentrix-slm -f models\Modelfile.sentrix
ollama run sentrix-slm
```

See `models/Modelfile.sentrix` for the template.

---

## Files

| File | Purpose |
|------|---------|
| `scripts/slm_dataset.py` | Generate SFT data from SENTRIX's policy |
| `scripts/slm_train.py` | LoRA fine-tune (CPU) → `models/sentrix-slm-lora/` |
| `scripts/slm_infer.py` | Evaluate the adapter vs ground truth |
| `agent/reasoning/slm_local.py` | Live local-SLM reasoning provider |
| `models/Modelfile.sentrix` | Ollama export template |
