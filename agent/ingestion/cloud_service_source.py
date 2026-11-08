"""
SENTRIX — legacy cloud_service_source shim.
Backed by the LogHub-grounded engine (dataset "openstack").
Kept so existing imports in api/main.py resolve unchanged.
"""

from agent.ingestion import loghub_engine

DATASET_ID = "openstack"

def _assets():
    return loghub_engine.get_assets_for_dataset(DATASET_ID)

# Static asset list (current world-tick snapshot at import time is fine;
# consumers that need live state call the engine directly).
CLOUD_ASSETS = _assets()
