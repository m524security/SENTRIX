"""
SENTRIX — LLM connectivity smoke test.
Tries your configured provider chain (OpenRouter / NVIDIA / Gemini / any
OpenAI-compatible endpoint) and reports which one answers. Run:  python test_llm.py
"""
import sys
sys.path.insert(0, ".")

from agent import config
from agent.reasoning import llm_providers

chain = config.active_provider_names()
print("Configured provider chain:", " -> ".join(chain) if chain else "(none — rule engine only)")

if not chain:
    print("No LLM key found. Add OPENROUTER_API_KEY or NVIDIA_API_KEY (both free) to .env.")
    print("SENTRIX still runs fully on its deterministic rule engine without any key.")
    sys.exit(0)

try:
    result = llm_providers.call_llm('Reply with exactly this JSON and nothing else: {"status": "ok"}')
    print(f"OK — answered by {result['provider']} ({result['model']})")
    print("Response:", result["text"][:200])
except Exception as e:
    print(f"All providers failed: {e}")
    print("Check your key(s) and network. SENTRIX will use the rule engine in the meantime.")
