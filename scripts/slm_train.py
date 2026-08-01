"""
SENTRIX — SLM LoRA Fine-Tuner
============================
Fine-tunes a small instruct model with LoRA so it reproduces SENTRIX's
recovery-risk decisions. Designed to run on CPU (no GPU/bitsandbytes needed).

Pipeline:
    1. scripts/slm_dataset.py   -> data/slm/train.jsonl, val.jsonl
    2. scripts/slm_train.py     -> models/sentrix-slm-lora/  (LoRA adapter)
    3. scripts/slm_infer.py     -> sanity-check the trained adapter

Defaults are tuned for "prove it works on a laptop CPU in minutes". Scale up
--max-steps / --base for a stronger model once you confirm the loop runs.

Run:
    venv\\Scripts\\python scripts\\slm_train.py --max-steps 200
    venv\\Scripts\\python scripts\\slm_train.py --base Qwen/Qwen2.5-1.5B-Instruct --epochs 2 --max-steps 0
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "slm"
OUT = ROOT / "models" / "sentrix-slm-lora"


def load_jsonl(path: Path):
    rows = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def main(args):
    import torch
    from datasets import Dataset
    from transformers import (AutoModelForCausalLM, AutoTokenizer,
                              DataCollatorForSeq2Seq, Trainer, TrainingArguments)
    from peft import LoraConfig, get_peft_model

    torch.manual_seed(args.seed)
    if not (DATA / "train.jsonl").exists():
        print("No dataset found. Run:  python scripts/slm_dataset.py --n 4000")
        sys.exit(1)

    print(f"Base model: {args.base}")
    tok = AutoTokenizer.from_pretrained(args.base)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    model = AutoModelForCausalLM.from_pretrained(args.base, torch_dtype=torch.float32)
    model.config.use_cache = False

    lora = LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_r * 2, lora_dropout=0.05, bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj"],
    )
    model = get_peft_model(model, lora)
    model.print_trainable_parameters()

    max_len = args.max_len

    def tokenize(example):
        msgs = example["messages"]
        # Render to strings first, then tokenize into plain int lists. Doing
        # tokenize=True directly can return a tokenizers.Encoding object that
        # Arrow/datasets can't serialize — this avoids that entirely.
        prompt_text = tok.apply_chat_template(msgs[:-1], add_generation_prompt=True, tokenize=False)
        full_text = tok.apply_chat_template(msgs, add_generation_prompt=False, tokenize=False)
        full_ids = tok(full_text, add_special_tokens=False, truncation=True, max_length=max_len)["input_ids"]
        prompt_ids = tok(prompt_text, add_special_tokens=False)["input_ids"]
        full_ids = [int(x) for x in full_ids]
        labels = list(full_ids)
        # Mask everything up to (and including) the prompt — train only on the answer.
        cut = min(len(prompt_ids), len(full_ids))
        for i in range(cut):
            labels[i] = -100
        return {"input_ids": full_ids, "attention_mask": [1] * len(full_ids), "labels": labels}

    train_rows = load_jsonl(DATA / "train.jsonl")
    val_rows = load_jsonl(DATA / "val.jsonl")
    train_ds = Dataset.from_list(train_rows).map(tokenize, remove_columns=["messages"])
    val_ds = Dataset.from_list(val_rows).map(tokenize, remove_columns=["messages"])
    print(f"Train {len(train_ds)} / Val {len(val_ds)} examples; max_len={max_len}")

    collator = DataCollatorForSeq2Seq(tok, model=model, padding=True, label_pad_token_id=-100)

    targs = TrainingArguments(
        output_dir=str(OUT / "_checkpoints"),
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        num_train_epochs=args.epochs,
        max_steps=args.max_steps if args.max_steps > 0 else -1,
        logging_steps=10,
        save_strategy="no",
        eval_strategy="no",
        report_to=[],
        dataloader_num_workers=0,
        use_cpu=not torch.cuda.is_available(),
    )

    trainer = Trainer(model=model, args=targs, train_dataset=train_ds,
                      eval_dataset=val_ds, data_collator=collator)
    print("\nTraining... (CPU — be patient; watch the loss fall)\n")
    trainer.train()

    OUT.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(OUT))
    tok.save_pretrained(str(OUT))
    (OUT / "sentrix_base.txt").write_text(args.base, encoding="utf-8")
    print(f"\nSaved LoRA adapter -> {OUT.relative_to(ROOT)}")
    print("Next:  python scripts/slm_infer.py")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="LoRA fine-tune SENTRIX SLM")
    ap.add_argument("--base", default="Qwen/Qwen2.5-0.5B-Instruct")
    ap.add_argument("--epochs", type=float, default=1.0)
    ap.add_argument("--max-steps", type=int, default=200,
                    help="cap steps for a fast CPU demo; set 0 to use full epochs")
    ap.add_argument("--batch", type=int, default=1)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--lora-r", type=int, default=8)
    ap.add_argument("--max-len", type=int, default=512)
    ap.add_argument("--seed", type=int, default=42)
    main(ap.parse_args())
