"""
SENTRIX — LogHub Sample Fetcher
===============================
Fixes the single most visible "it looks broken on clone" problem: the real-data
grounding (calibrated error rates, real evidence lines, the failure digest fed
to the reasoning model) is empty until the LogHub sample logs exist locally, and
nothing previously told a new user to download them.

This script pulls ONLY the small `*_2k.log_structured.csv` samples SENTRIX
actually parses (a few MB total, not the 150MB+ full corpus) from the public
logpai/loghub mirror, into ./loghub/<Dataset>/.

Usage:
    python scripts/fetch_loghub.py                 # all 16 datasets
    python scripts/fetch_loghub.py --only HDFS Apache Windows

Idempotent — files already present are skipped. If a source path 404s (mirror
layout changes over time), it's reported and skipped; SENTRIX still runs fully
without it (it just falls back to synthetic calibration for that dataset).
"""

from __future__ import annotations

import argparse
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGHUB_DIR = ROOT / "loghub"

# Dataset folder names SENTRIX's engine expects (see loghub_engine.DATASET_DEFS).
DATASETS = [
    "HDFS", "Apache", "Windows", "OpenSSH", "OpenStack", "Linux", "BGL",
    "Zookeeper", "Spark", "Hadoop", "Proxifier", "Mac", "Thunderbird",
    "HPC", "Android", "HealthApp",
]

# Public raw mirror. The 2k structured samples live under loghub/<Dataset>/.
BASES = [
    "https://raw.githubusercontent.com/logpai/loghub/master/{ds}/{ds}_2k.log_structured.csv",
    "https://raw.githubusercontent.com/logpai/loghub/master/{ds}/{ds}_2k.log_structured.csv?raw=true",
]


def fetch_one(ds: str) -> str:
    dest_dir = LOGHUB_DIR / ds
    dest = dest_dir / f"{ds}_2k.log_structured.csv"
    if dest.exists() and dest.stat().st_size > 0:
        return "skip"
    dest_dir.mkdir(parents=True, exist_ok=True)
    for base in BASES:
        url = base.format(ds=ds)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sentrix-fetch/4.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
            if data and b"," in data[:200]:
                dest.write_bytes(data)
                return f"ok ({len(data)//1024} KB)"
        except Exception:
            continue
    return "404 / unavailable"


def main():
    ap = argparse.ArgumentParser(description="Fetch LogHub 2k sample logs for SENTRIX grounding.")
    ap.add_argument("--only", nargs="*", help="subset of dataset folder names (e.g. HDFS Apache)")
    args = ap.parse_args()

    targets = args.only or DATASETS
    print(f"SENTRIX :: fetching {len(targets)} LogHub sample dataset(s) into {LOGHUB_DIR}\n")
    ok = skipped = failed = 0
    for ds in targets:
        status = fetch_one(ds)
        if status == "skip":
            skipped += 1; mark = "="
        elif status.startswith("ok"):
            ok += 1; mark = "+"
        else:
            failed += 1; mark = "!"
        print(f"  [{mark}] {ds:<12} {status}")

    print(f"\nDone — {ok} downloaded, {skipped} already present, {failed} unavailable.")
    if failed:
        print("Unavailable datasets fall back to synthetic calibration; SENTRIX still runs.")
    if ok or skipped:
        print("Grounding active. Start the API and check /api/datasets to see real error rates.")
    return 0 if (ok or skipped) else 1


if __name__ == "__main__":
    sys.exit(main())
