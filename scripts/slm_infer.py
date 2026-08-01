"""
SENTRIX — SLM Inference / Sanity Check
=====================================
Loads the fine-tuned LoRA adapter on top of its base model and runs SENTRIX's
recovery-risk reasoning on a few asset states, comparing the SLM's decision
against the deterministic ground truth so you can see how well it learned.

Run:
    venv\\Scripts\\python scripts\\slm_infer.py
    venv\\Scripts\\python scripts\\slm_infer.py --base-only   # un-finetuned baseline
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ADAPTER = ROOT / "models" / "sentrix-slm-lora"

from agent.reasoning.reasoning_core import compute_risk, decide_action
from scripts.slm_dataset import SLM_SYSTEM_PROMPT

SAMPLES = [
    {"asset_id": "ASSET-9001", "asset_name": "SAP ERP Production", "tier": 1,
     "criticality_score": 95, "rpo_target_hours": 4, "hours_since_last_backup": 19.0,
     "consecutive_failures": 0, "restore_test_days_overdue": 0,
     "log_evidence": "ERROR repository offline — last good restore point exceeds RPO target"},
    {"asset_id": "ASSET-9002", "asset_name": "Dev Server 01", "tier": 4,
     "criticality_score": 15, "rpo_target_hours": 72, "hours_since_last_backup": 50.0,
     "consecutive_failures": 0, "restore_test_days_overdue": 0,
     "log_evidence": "INFO snapshot OK, RPO well within target"},
    {"asset_id": "ASSET-9003", "asset_name": "Payroll Database", "tier": 1,
     "criticality_score": 92, "rpo_target_hours": 4, "hours_since_last_backup": 3.0,
     "consecutive_failures": 3, "restore_test_days_overdue": 0,
     "log_evidence": "FATAL backup agent unreachable; snapshot aborted after 3 retries"},
    {"asset_id": "ASSET-9004", "asset_name": "NAS File Share", "tier": 3,
     "criticality_score": 40, "rpo_target_hours": 24, "hours_since_last_backup": 10.0,
     "consecutive_failures": 0, "restore_test_days_overdue": 22,
     "log_evidence": "INFO restore-test interval exceeded; integrity proof stale"},
]


def main(args):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    if args.base_only:
        base = args.base
        print(f"Loading BASE only: {base}")
        tok = AutoTokenizer.from_pretrained(base)
        model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=torch.float32)
    else:
        if not ADAPTER.exists():
            print("No adapter found. Train first:  python scripts/slm_train.py")
            sys.exit(1)
        from peft import PeftModel
        base = (ADAPTER / "sentrix_base.txt").read_text(encoding="utf-8").strip()
        print(f"Loading base {base} + LoRA adapter {ADAPTER.name}")
        tok = AutoTokenizer.from_pretrained(str(ADAPTER))
        model = AutoModelForCausalLM.from_pretrained(base, torch_dtype=torch.float32)
        model = PeftModel.from_pretrained(model, str(ADAPTER))
    model.eval()

    correct = 0
    for s in SAMPLES:
        user = json.dumps({k: s[k] for k in (
            "asset_id", "asset_name", "tier", "criticality_score", "rpo_target_hours",
            "hours_since_last_backup", "consecutive_failures", "restore_test_days_overdue",
            "log_evidence")}, separators=(",", ":"))
        msgs = [{"role": "system", "content": SLM_SYSTEM_PROMPT},
                {"role": "user", "content": user}]
        ids = tok.apply_chat_template(msgs, add_generation_prompt=True,
                                      return_tensors="pt", return_dict=True)
        in_len = ids["input_ids"].shape[1]
        with torch.no_grad():
            out = model.generate(**ids, max_new_tokens=160, do_sample=False,
                                 pad_token_id=tok.pad_token_id or tok.eos_token_id)
        text = tok.decode(out[0][in_len:], skip_special_tokens=True).strip()

        risk = compute_risk(s)
        truth = decide_action(s, risk)
        try:
            pred = json.loads(text[text.find("{"): text.rfind("}") + 1])
            pred_action = pred.get("action", "?")
        except Exception:
            pred_action = "(unparseable)"
        ok = pred_action == truth
        correct += ok
        print("\n" + "-" * 70)
        print(f"{s['asset_name']} (tier {s['tier']}, {s['hours_since_last_backup']}h / "
              f"{s['rpo_target_hours']}h RPO, {s['consecutive_failures']} fails)")
        print(f"  ground truth action : {truth}  (risk {risk['risk_score']})")
        print(f"  SLM action          : {pred_action}  {'OK' if ok else 'MISMATCH'}")
        print(f"  SLM raw             : {text[:200]}")

    print("\n" + "=" * 70)
    print(f"Action accuracy on samples: {correct}/{len(SAMPLES)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-only", action="store_true", help="run the un-finetuned base model")
    ap.add_argument("--base", default="Qwen/Qwen2.5-0.5B-Instruct")
    main(ap.parse_args())
