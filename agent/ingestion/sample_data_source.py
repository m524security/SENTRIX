"""
SENTRIX — Data Source Facade (compatibility layer)
-------------------------------------------------
Historically the agent and API imported asset state from this module and a
pile of per-source modules, each of which re-rolled random numbers on every
call. All of that is now backed by `loghub_engine`, which is deterministic
within a world tick and calibrated from the real LogHub datasets.

This module preserves the original public names so nothing else has to
change, while delegating every call to the single grounded engine.
"""

from agent.ingestion import loghub_engine


def get_all_asset_states() -> list:
    return loghub_engine.get_all_assets()


# Original agent loop called this name.
def get_current_asset_states() -> list:
    return loghub_engine.get_all_assets()


def get_asset_states_by_dataset(dataset_id: str) -> list:
    return loghub_engine.get_assets_for_dataset(dataset_id)


def get_asset_state(asset_id: str):
    return loghub_engine.get_asset_by_id(asset_id)


# DATASET_REGISTRY used to be a dict of {id: {..., "fetcher": fn}}. The API
# strips "fetcher" before serialising, so we expose the same shape minus the
# unused callable.
def _build_registry():
    return loghub_engine.dataset_registry()


DATASET_REGISTRY = _build_registry()
