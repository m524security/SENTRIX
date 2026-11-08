"""
Extract failure patterns from LogHub HDFS dataset
and convert them to SENTRIX training scenarios.
"""

import csv
import re
from pathlib import Path

def extract_hdfs_patterns(log_path: str, label_path: str) -> list:
    """
    Reads LogHub HDFS logs and finds real failure patterns.
    Returns list of failure signatures for SENTRIX prompt examples.
    """
    
    # Read anomaly labels (which block IDs are failures)
    anomaly_blocks = set()
    with open(label_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get('Label') == 'Anomaly':
                anomaly_blocks.add(row['BlockId'])
    
    print(f"Found {len(anomaly_blocks)} anomalous block IDs in HDFS dataset")
    
    # Read logs and find patterns around anomalous blocks
    failure_patterns = []
    pattern_counts = {}
    
    with open(log_path, 'r', errors='ignore') as f:
        for line in f:
            # Check if line contains a known anomalous block
            for block_id in anomaly_blocks:
                if block_id in line:
                    # Extract the log message template
                    # Remove timestamps and block IDs to get the pattern
                    pattern = re.sub(r'\d{6} \d{6} \d+ ', '', line)
                    pattern = re.sub(r'blk_-?\d+', 'blk_ID', pattern)
                    pattern = re.sub(r'\d+\.\d+\.\d+\.\d+', 'IP', pattern)
                    pattern = pattern.strip()
                    
                    if pattern:
                        pattern_counts[pattern] = pattern_counts.get(pattern, 0) + 1
    
    # Get top 20 most common failure patterns
    top_patterns = sorted(pattern_counts.items(), key=lambda x: x[1], reverse=True)[:20]
    
    return [{"pattern": p, "frequency": c} for p, c in top_patterns]


def convert_to_sentrix_scenarios(patterns: list) -> list:
    """
    Convert HDFS failure patterns to SENTRIX test scenarios.
    Maps system log anomalies to backup failure scenarios.
    """
    scenarios = []
    
    for i, p in enumerate(patterns):
        scenario = {
            "scenario_id": f"TRAIN-{str(i+1).zfill(3)}",
            "source": "LogHub HDFS",
            "failure_pattern": p["pattern"],
            "frequency_in_dataset": p["frequency"],
            # Map to SENTRIX asset state
            "asset_state": {
                "asset_id": f"HDFS-NODE-{str(i+1).zfill(2)}",
                "asset_name": f"Hadoop Node {i+1}",
                "tier": 2,
                "criticality_score": 70,
                "rpo_target_hours": 8,
                "hours_since_last_backup": 8 + (i * 2),  # progressively worse
                "consecutive_failures": min(i // 3, 5),
                "last_backup_status": "failed",
                "restore_test_days_overdue": 0
            },
            # Expected SENTRIX decision
            "expected_action": "ESCALATE_P1" if i < 3 else "ESCALATE_P2" if i < 8 else "WARN"
        }
        scenarios.append(scenario)
    
    return scenarios


if __name__ == "__main__":
    log_path   = "loghub/HDFS_v1/HDFS.log"
    label_path = "loghub/HDFS_v1/anomaly_label.csv"
    
    if not Path(log_path).exists():
        print("LogHub data not found. Run: git clone https://github.com/logpai/loghub first")
        exit(1)
    
    patterns = extract_hdfs_patterns(log_path, label_path)
    scenarios = convert_to_sentrix_scenarios(patterns)
    
    import json
    with open("tests/scenarios/hdfs_training_scenarios.json", "w") as f:
        json.dump(scenarios, f, indent=2)
    
    print(f"✓ Extracted {len(patterns)} failure patterns")
    print(f"✓ Created {len(scenarios)} training scenarios")
    print(f"  Saved to: tests/scenarios/hdfs_training_scenarios.json")