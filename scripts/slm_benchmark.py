"""
SENTRIX — SLM Accuracy Benchmark
===============================
Evaluates the locally fine-tuned SLM on a held-out validation set and reports
honest, reproducible metrics for the README:

  - JSON validity rate   (did the model emit parseable SENTRIX JSON?)
  - Action accuracy       (did the SLM's action match ground-truth policy?)
  - Per-action breakdown
  - Mean latency per decision (CPU)
  - Optional base-model comparison to quantify the fine-tune's lift

Ground truth lives in each val example's assistant message (generated from
SENTRIX's deterministic compute_risk/decide_action), so this is a real held-out
test, not a self-graded one.

Run:
    venv\\Scripts\\python scripts\\slm_benchmark.py --n 60
    venv\\Scripts\\python scripts\\slm_benchmark.py --n 60 --compare-base
"""

from __future__ import annotations

import argparse
import json
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAL = ROOT / "data" / "slm" / "val.jsonl"
ADAPTER = ROOT / "models" / "sentrix-slm-lora"


def _load_val(n):
    rows = []
    with VAL.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows[:n]


def _action_of(text):
    try:
        obj = json.loads(text[text.find("{"): text.rfind("}") + 1])
        return obj.get("action"), True
    except Exception:
        return None, False


def _evaluate(model, tok, rows, label):
    import torch
    correct = json_ok = 0
    per_action = defaultdict(lambda: [0, 0])  # action -> [correct, total]
    latencies = []
    for r in rows:
        msgs = r["messages"]
        truth, _ = _action_of(msgs[-1]["content"])
        ids = tok.apply_chat_template(msgs[:-1], add_generation_prompt=True,
                                      return_tensors="pt", return_dict=True)
        in_len = ids["input_ids"].shape[1]
        t0 = time.time()
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=128, do_sample=False,
                                 pad_token_id=tok.pad_token_id or tok.eos_token_id)
        latencies.append(time.time() - t0)
        pred, ok = _action_of(tok.decode(out[0][in_len:], skip_special_tokens=True))
        json_ok += ok
        per_action[truth][1] += 1
        if pred == truth:
            correct += 1
            per_action[truth][0] += 1

    n = len(rows)
    print(f"\n=== {label} (n={n}) ===")
    print(f"  JSON validity   : {json_ok}/{n}  ({json_ok/n*100:.1f}%)")
    print(f"  Action accuracy : {correct}/{n}  ({correct/n*100:.1f}%)")
    print(f"  Mean latency    : {sum(latencies)/n:.2f}s/decision (CPU)")
    print("  Per-action:")
    for act in sorted(per_action):
        c, t = per_action[act]
        print(f"    {act:24} {c}/{t}  ({(c/t*100 if t else 0):.0f}%)")
    return {"json": json_ok / n, "acc": correct / n, "lat": sum(latencies) / n}


def main(args):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    rows = _load_val(args.n)
    base = (ADAPTER / "sentrix_base.txt").read_text(encoding="utf-8").strip()

    if args.compare_base:
        print(f"Loading BASE {base} (un-finetuned)")
        tokb = AutoTokenizer.from_pretrained(base)
        if tokb.pad_token is None:
            tokb.pad_token = tokb.eos_token
        mb = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float32).eval()
        base_res = _evaluate(mb, tokb, rows, "BASE (no fine-tune)")
        del mb

    from peft import PeftModel
    print(f"Loading FINE-TUNED {base} + LoRA")
    tok = AutoTokenizer.from_pretrained(str(ADAPTER))
    m = AutoModelForCausalLM.from_pretrained(base, dtype=torch.float32)
    m = PeftModel.from_pretrained(m, str(ADAPTER)).eval()
    ft_res = _evaluate(m, tok, rows, "FINE-TUNED (SENTRIX LoRA)")

    if args.compare_base:
        lift = (ft_res["acc"] - base_res["acc"]) * 100
        print(f"\n>>> Fine-tune lift: {base_res['acc']*100:.1f}% -> {ft_res['acc']*100:.1f}% "
              f"action accuracy ({lift:+.1f} pts)")
    print("\nNote: with SENTRIX_SLM_STRICT=true the agent's *actions* are 100% "
          "policy-compliant regardless (out-of-policy generations are snapped).")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--compare-base", action="store_true")
    main(ap.parse_args())
