# agent/ingestion/aws_backup_client.py
# Requires: pip install boto3

import boto3
from datetime import datetime, timedelta
from dotenv import load_dotenv
import os

from agent.logging_setup import get_logger

_log = get_logger("aws_backup")

load_dotenv()

def get_aws_backup_jobs(hours_back: int = 2) -> list:
    """
    Fetch real backup job status from AWS Backup.
    Uses your AWS credentials from .env file.
    AWS Free Tier includes AWS Backup API calls.
    """
    client = boto3.client(
        "backup",
        region_name=os.getenv("AWS_REGION", "ap-south-1"),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY")
    )
    
    start_time = datetime.utcnow() - timedelta(hours=hours_back)
    
    try:
        response = client.list_backup_jobs(
            ByCreatedAfter=start_time,
            MaxResults=100
        )
        
        jobs = []
        for job in response.get("BackupJobs", []):
            jobs.append({
                "backup_job_id": job["BackupJobId"],
                "asset_id": job.get("ResourceArn", "").split(":")[-1],
                "source_type": "cloud_aws",
                "backup_type": "snapshot",
                "status": job["State"].lower().replace("completed", "success"),
                "started_at": job.get("CreationDate", "").isoformat() if hasattr(job.get("CreationDate",""), "isoformat") else str(job.get("CreationDate","")),
                "completed_at": job.get("CompletionDate", ""),
                "data_size_gb": round(job.get("BackupSizeInBytes", 0) / (1024**3), 2),
            })
        
        return jobs
        
    except Exception as e:
        _log.error("AWS Backup API error: %s", e)
        return []

# Add to .env:
# AWS_ACCESS_KEY_ID=your_key
# AWS_SECRET_ACCESS_KEY=your_secret
# AWS_REGION=ap-south-1 (Mumbai — closest to India)