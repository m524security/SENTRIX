"""
SENTRIX — Sample Data Generator
Creates realistic fake backup data for development and demo.
Run: python scripts/generate_sample_data.py --assets 20 --days 30
"""

import argparse
import json
import random
import uuid
from datetime import datetime, timedelta
from supabase import create_client
from dotenv import load_dotenv
import os

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

load_dotenv()
supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

ASSET_TEMPLATES = [
    {"name": "SAP ERP Production",    "type": "database", "tier": 1, "crit": 95, "rpo": 4,  "rto": 60},
    {"name": "CRM Database Primary",  "type": "database", "tier": 1, "crit": 90, "rpo": 4,  "rto": 60},
    {"name": "Oracle Finance DB",     "type": "database", "tier": 1, "crit": 88, "rpo": 4,  "rto": 60},
    {"name": "Payroll Database",      "type": "database", "tier": 1, "crit": 92, "rpo": 4,  "rto": 60},
    {"name": "Exchange Mail Server",  "type": "vm",       "tier": 2, "crit": 70, "rpo": 8,  "rto": 120},
    {"name": "HR SQL Server",         "type": "database", "tier": 2, "crit": 65, "rpo": 8,  "rto": 120},
    {"name": "K8s Production Cluster","type": "container","tier": 2, "crit": 80, "rpo": 2,  "rto": 30},
    {"name": "NAS File Share",        "type": "nas",      "tier": 3, "crit": 40, "rpo": 24, "rto": 240},
    {"name": "MongoDB Analytics",     "type": "database", "tier": 3, "crit": 35, "rpo": 24, "rto": 240},
    {"name": "Dev Server 01",         "type": "vm",       "tier": 4, "crit": 15, "rpo": 72, "rto": 480},
]

def generate_assets(count: int) -> list:
    assets = []
    for i in range(min(count, len(ASSET_TEMPLATES))):
        t = ASSET_TEMPLATES[i]
        asset = {
            "asset_id": f"ASSET-{str(i+1).zfill(3)}",
            "asset_name": t["name"],
            "asset_type": t["type"],
            "environment": "production",
            "tier": t["tier"],
            "business_owner": f"owner{i+1}@company.com",
            "it_owner": f"itowner{i+1}@company.com",
            "rpo_target_hours": t["rpo"],
            "rto_target_minutes": t["rto"],
            "backup_frequency_hours": t["rpo"],
            "required_retention_days": 30 if t["tier"] <= 2 else 14,
            "required_restore_test_days": 30,
            "criticality_score": t["crit"],
            "data_classification": "confidential" if t["tier"] <= 2 else "internal",
            "compliance_frameworks": ["ISO27001"] if t["tier"] <= 2 else []
        }
        assets.append(asset)
    return assets

def generate_backup_jobs(asset: dict, days: int) -> list:
    jobs = []
    now = datetime.utcnow()
    freq_hours = asset["rpo_target_hours"]
    
    current = now - timedelta(days=days)
    while current < now:
        # 95% success rate for Tier 1, 92% for others
        success_rate = 0.95 if asset["tier"] == 1 else 0.92
        status = "success" if random.random() < success_rate else "failed"
        
        duration_mins = random.randint(20, 180)
        completed = current + timedelta(minutes=duration_mins) if status == "success" else None
        
        job = {
            "backup_job_id": str(uuid.uuid4()),
            "asset_id": asset["asset_id"],
            "source_type": "sample",
            "backup_type": "incremental" if random.random() > 0.1 else "full",
            "status": status,
            "started_at": current.isoformat(),
            "completed_at": completed.isoformat() if completed else None,
            "data_size_gb": round(random.uniform(10, 500), 2),
            "checksum_hash": str(uuid.uuid4()).replace("-", "") if status == "success" else None,
            "storage_location": f"s3://sentrix-backups/{asset['asset_id']}/{current.strftime('%Y%m%d')}",
            "error_code": None if status == "success" else "ERR_AGENT_TIMEOUT",
            "error_message": None if status == "success" else "Backup agent did not respond",
            "rpo_at_completion_hrs": round(freq_hours * random.uniform(0.1, 0.8), 2),
            "window_compliance": True
        }
        jobs.append(job)
        current += timedelta(hours=freq_hours)
    
    return jobs

def main(asset_count: int, days: int):
    print(f"SENTRIX Sample Data Generator")
    print(f"Generating {asset_count} assets with {days} days of history...")
    
    assets = generate_assets(asset_count)
    
    # Insert assets
    supabase.table("asset_criticality").upsert(assets).execute()
    print(f"✓ Inserted {len(assets)} assets")
    
    # Insert backup jobs
    total_jobs = 0
    for asset in assets:
        jobs = generate_backup_jobs(asset, days)
        # Insert in batches of 100
        for i in range(0, len(jobs), 100):
            batch = jobs[i:i+100]
            supabase.table("backup_job_logs").upsert(batch).execute()
        total_jobs += len(jobs)
        print(f"  ✓ {asset['asset_name']}: {len(jobs)} backup jobs generated")
    
    print(f"\n✅ Done! Generated {len(assets)} assets and {total_jobs} backup jobs")
    print(f"   View your data at: {os.getenv('SUPABASE_URL')}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate SENTRIX sample data")
    parser.add_argument("--assets", type=int, default=10, help="Number of assets")
    parser.add_argument("--days",   type=int, default=30, help="Days of history")
    args = parser.parse_args()
    main(args.assets, args.days)