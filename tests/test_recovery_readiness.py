"""
SENTRIX — Recovery Readiness Tests (PS284 core)
===============================================
Pins the behaviour that makes SENTRIX answer PS284 rather than being another
backup dashboard:

  • evidence decays exponentially, per tier
  • a failed drill is strong evidence of NON-recoverability
  • drift erodes confidence even when backups are green
  • the scheduler covers critical blind spots before cheap wins
  • the ledger is tamper-evident
  • the loop closes: recording a test re-scores the asset
"""
import pytest

from agent.recovery import confidence as prc
from agent.recovery import evidence_ledger as ledger
from agent.recovery import evidence_scheduler as sched


def _asset(**kw):
    base = {
        "asset_id": "test-01", "asset_name": "Test Asset", "tier": 1,
        "hours_since_last_backup": 2, "rpo_target_hours": 4,
        "consecutive_failures": 0, "criticality_score": 90,
        "last_restore_test": {"type": "full_restore_drill", "days_ago": 5, "passed": True},
        "config_changes_since_restore_test": 0,
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# The headline claim
# --------------------------------------------------------------------------- #
def test_green_backup_can_be_an_unproven_recovery():
    """THE thesis. Backup 2h old against a 4h RPO, zero failures — green on
    every commercial dashboard — but untested for 214 days with heavy drift
    must NOT read as recoverable."""
    a = _asset(last_restore_test={"type": "full_restore_drill", "days_ago": 214, "passed": True},
               config_changes_since_restore_test=47)
    s = prc.score_asset(a)
    assert s["confidence"] < 0.35
    assert s["band"] == "blind_spot"
    assert s["components"]["backup_freshness"] > 0.4   # backups genuinely fine
    assert s["components"]["evidence"] < 0.05          # evidence rotted away


def test_fresh_drill_scores_high():
    """Everything fresh: backup just taken, drill passed yesterday, no drift."""
    s = prc.score_asset(_asset(hours_since_last_backup=0.2,
                               last_restore_test={"type": "full_restore_drill",
                                                  "days_ago": 1, "passed": True}))
    assert s["confidence"] > 0.9
    assert s["band"] == "proven"


def test_half_consumed_rpo_reads_as_probable_not_proven():
    """Calibration check: a backup halfway through its RPO window with 5-day-old
    evidence is 'probable', not 'proven'. The model should not flatter."""
    s = prc.score_asset(_asset())   # 2h into a 4h RPO
    assert 0.7 < s["confidence"] < 0.85
    assert s["band"] == "probable"


# --------------------------------------------------------------------------- #
# Evidence decay
# --------------------------------------------------------------------------- #
def test_evidence_decays_exponentially():
    d0 = prc.evidence_decay_factor(0, 1)
    d30 = prc.evidence_decay_factor(30, 1)   # tier-1 half-life is 30d
    d60 = prc.evidence_decay_factor(60, 1)
    assert d0 == pytest.approx(1.0)
    assert d30 == pytest.approx(0.5, abs=0.01)
    assert d60 == pytest.approx(0.25, abs=0.01)


def test_decay_is_tier_specific():
    """A tier-1 transactional DB drifts faster than a tier-4 cold archive, so
    identical evidence ages differently. This is the novel parameter."""
    assert prc.evidence_decay_factor(60, 1) < prc.evidence_decay_factor(60, 4)


def test_evidence_strength_is_graded():
    """A checksum verify is not a recovery drill."""
    drill = prc.evidence_component("full_restore_drill", 0, 1)
    partial = prc.evidence_component("partial_restore", 0, 1)
    checksum = prc.evidence_component("checksum_verify", 0, 1)
    assert drill > partial > checksum > 0


def test_failed_drill_is_evidence_of_non_recoverability():
    """A failed test is not weak positive evidence — it floors the component."""
    assert prc.evidence_component("full_restore_drill", 1, 1, passed=False) == 0.0
    a = _asset(last_restore_test={"type": "full_restore_drill", "days_ago": 1, "passed": False})
    assert prc.score_asset(a)["confidence"] < 0.6


def test_never_tested_has_no_evidence():
    a = _asset(last_restore_test={"type": "none", "days_ago": 0, "passed": False})
    s = prc.score_asset(a)
    assert s["components"]["evidence"] == 0.0
    assert "no successful restore test on record" in s["gaps"]


# --------------------------------------------------------------------------- #
# Drift
# --------------------------------------------------------------------------- #
def test_drift_erodes_confidence_even_with_green_backups():
    clean = prc.score_asset(_asset(config_changes_since_restore_test=0))
    drifted = prc.score_asset(_asset(config_changes_since_restore_test=50))
    assert drifted["confidence"] < clean["confidence"]


def test_drift_penalty_saturates():
    """The 50th change matters less than the 5th, but never exceeds the ceiling."""
    p5, p50, p500 = (prc.drift_penalty(n) for n in (5, 50, 500))
    assert p5 < p50 < p500 <= prc.DRIFT_PENALTY_CEILING
    assert (p50 - p5) > (p500 - p50)


# --------------------------------------------------------------------------- #
# Chain integrity + determinism
# --------------------------------------------------------------------------- #
def test_broken_chain_zeroes_integrity():
    assert prc.chain_integrity(3) == 0.0
    assert prc.chain_integrity(0) == 1.0


def test_scoring_is_deterministic():
    """No LLM touches the number — same input, same output, always."""
    a = _asset()
    runs = [prc.score_asset(a)["confidence"] for _ in range(5)]
    assert len(set(runs)) == 1


def test_confidence_interval_widens_with_staleness():
    narrow = prc.confidence_interval(1, 1, True)
    wide = prc.confidence_interval(200, 1, True)
    untested = prc.confidence_interval(0, 1, False)
    assert narrow < wide
    assert untested >= wide


# --------------------------------------------------------------------------- #
# Fleet scoring
# --------------------------------------------------------------------------- #
def test_fleet_confidence_is_criticality_weighted():
    """A proven cold archive must not offset an unproven payments DB."""
    critical_bad = _asset(asset_id="pay", tier=1, criticality_score=100,
                          last_restore_test={"type": "none", "days_ago": 0, "passed": False})
    trivial_good = _asset(asset_id="arc", tier=4, criticality_score=5)
    fleet = prc.score_fleet([critical_bad, trivial_good])
    naive_mean = sum(s["confidence"] for s in fleet["assets"]) / 2
    assert fleet["fleet_confidence"] < naive_mean


def test_fleet_reports_blind_spots():
    f = prc.score_fleet([
        _asset(asset_id="a", last_restore_test={"type": "none", "days_ago": 0, "passed": False}),
        _asset(asset_id="b"),
    ])
    assert f["blind_spot_count"] >= 1


# --------------------------------------------------------------------------- #
# The agentic scheduler
# --------------------------------------------------------------------------- #
def test_scheduler_covers_critical_blind_spots_before_cheap_wins():
    """Regression for a real modelling flaw: pure value-per-hour greedy
    scheduled 15 tier-4 checksum verifies and deferred an unproven tier-1
    database. Cheap tests win on ratio; that is the wrong answer."""
    critical = _asset(asset_id="payments-db", tier=1, criticality_score=99,
                      last_restore_test={"type": "full_restore_drill", "days_ago": 300, "passed": True},
                      config_changes_since_restore_test=60)
    cheap = [_asset(asset_id=f"iot-{i}", tier=4, criticality_score=10,
                    rpo_target_hours=168, hours_since_last_backup=20,
                    last_restore_test={"type": "checksum_verify", "days_ago": 100, "passed": True},
                    config_changes_since_restore_test=2)
             for i in range(20)]
    p = sched.plan([critical] + cheap, budget_hours=6.0)
    ids = [c["asset_id"] for c in p["scheduled"]]
    assert "payments-db" in ids
    assert p["scheduled"][0]["asset_id"] == "payments-db"
    assert p["scheduled"][0]["tranche"] == "mandatory"


def test_scheduler_respects_budget():
    assets = [_asset(asset_id=f"a-{i}", tier=1,
                     last_restore_test={"type": "none", "days_ago": 0, "passed": False})
              for i in range(10)]
    p = sched.plan(assets, budget_hours=12.0)
    assert p["spent_hours"] <= 12.0


def test_scheduler_reports_unfunded_critical_work():
    """An agent that silently drops a tier-1 blind spot when the budget runs
    out is lying by omission."""
    assets = [_asset(asset_id=f"db-{i}", tier=1, criticality_score=95,
                     last_restore_test={"type": "none", "days_ago": 0, "passed": False})
              for i in range(10)]
    p = sched.plan(assets, budget_hours=6.0)   # only funds one drill
    assert p["unfunded_critical_count"] >= 8
    assert "WARNING" in sched.explain_plan(p)


def test_scheduler_skips_already_proven_assets():
    """Don't burn budget retesting what is already proven and fresh."""
    proven = _asset(asset_id="fresh", hours_since_last_backup=0.2,
                    last_restore_test={"type": "full_restore_drill",
                                       "days_ago": 1, "passed": True})
    assert prc.score_asset(proven)["confidence"] >= sched.RETEST_CONFIDENCE_CEILING
    p = sched.plan([proven], budget_hours=24.0)
    assert not p["scheduled"]


def test_scheduler_projects_confidence_uplift():
    a = _asset(asset_id="x", last_restore_test={"type": "none", "days_ago": 0, "passed": False})
    p = sched.plan([a], budget_hours=24.0)
    assert p["fleet_confidence_after"] > p["fleet_confidence_before"]
    assert p["confidence_uplift_pct"] > 0


def test_marginal_value_prefers_high_criticality_on_equal_gain():
    hi = sched.marginal_value(_asset(asset_id="h", criticality_score=100,
                                     last_restore_test={"type": "none", "days_ago": 0, "passed": False}))
    lo = sched.marginal_value(_asset(asset_id="l", criticality_score=10,
                                     last_restore_test={"type": "none", "days_ago": 0, "passed": False}))
    assert hi["weighted_gain"] > lo["weighted_gain"]


# --------------------------------------------------------------------------- #
# Evidence ledger
# --------------------------------------------------------------------------- #
def test_ledger_rejects_invalid_test_type():
    with pytest.raises(ValueError):
        ledger.record_test("a-1", "vibes_check", "passed")


def test_ledger_rejects_invalid_outcome():
    with pytest.raises(ValueError):
        ledger.record_test("a-1", "checksum_verify", "probably_fine")


def test_ledger_records_are_signed_and_verifiable(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ev.jsonl")
    ledger.reset()
    rec = ledger.record_test("a-1", "full_restore_drill", "passed", rto_actual_seconds=600)
    assert rec["signature"]
    assert ledger.verify_signature(rec)


def test_tampered_evidence_fails_verification(tmp_path, monkeypatch):
    """Restore evidence is exactly what gets quietly edited after a bad audit."""
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ev.jsonl")
    ledger.reset()
    rec = ledger.record_test("a-1", "checksum_verify", "failed")
    rec["outcome"] = "passed"           # attacker rewrites history
    assert not ledger.verify_signature(rec)


def test_loop_closes_recording_a_test_rescores_the_asset(tmp_path, monkeypatch):
    """The whole point: the agent acts, evidence lands, confidence re-scores."""
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ev.jsonl")
    ledger.reset()
    a = _asset(asset_id="loop-1",
               last_restore_test={"type": "none", "days_ago": 0, "passed": False})
    before = prc.score_asset(a)["confidence"]

    ledger.record_test("loop-1", "full_restore_drill", "passed", rto_actual_seconds=300)
    enriched = ledger.enrich([a])[0]
    after = prc.score_asset(enriched)["confidence"]

    assert after > before
    assert enriched["evidence_source"] == "ledger"


def test_enrich_falls_back_to_simulated_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "empty.jsonl")
    ledger.reset()
    out = ledger.enrich([{"asset_id": "never-seen", "tier": 2,
                          "restore_test_days_overdue": 5}])[0]
    assert out["evidence_source"] == "simulated"
    assert out["last_restore_test"]["simulated"] is True


# --------------------------------------------------------------------------- #
# Falsifiability
# --------------------------------------------------------------------------- #
def test_calibration_reports_insufficient_data_honestly():
    r = prc.calibrate_lambda([{"days_since_prior_test": 10, "restore_succeeded": True}], tier=1)
    assert r["ok"] is False
    assert "insufficient" in r["reason"]


def test_calibration_recovers_a_known_lambda():
    """λ is a claim, not a magic number. Synthesise decay with a known
    half-life and check the fitter finds it."""
    import math
    true_half_life = 40.0
    lam = math.log(2) / true_half_life
    history = []
    for bucket in range(0, 120, 15):
        rate = math.exp(-lam * bucket)
        n = 40
        successes = round(rate * n)
        for i in range(n):
            history.append({"days_since_prior_test": bucket + 1,
                            "restore_succeeded": i < successes})
    r = prc.calibrate_lambda(history, tier=1)
    assert r["ok"] is True
    assert r["fitted_half_life_days"] == pytest.approx(true_half_life, rel=0.25)


# --------------------------------------------------------------------------- #
# Dashboard support
# --------------------------------------------------------------------------- #
def test_decay_curve_spans_past_and_future():
    c = prc.decay_curve(_asset(last_restore_test={"type": "full_restore_drill",
                                                  "days_ago": 30, "passed": True}))
    assert c["points"][0]["days_from_now"] < 0      # back to the last test
    assert c["points"][-1]["days_from_now"] > 0     # forward into decay
    firsts = [p["confidence_pct"] for p in c["points"][:3]]
    lasts = [p["confidence_pct"] for p in c["points"][-3:]]
    assert max(firsts) > max(lasts)                 # confidence falls over time


# --------------------------------------------------------------------------- #
# Byte-level integrity signals (ransomware-relevant)
# --------------------------------------------------------------------------- #
def test_entropy_distinguishes_plaintext_from_ciphertext():
    import os
    plain = b"INSERT INTO payments VALUES (1, 'alice', 42.00);\n" * 400
    cipher = os.urandom(4000)
    assert ledger.shannon_entropy(plain) < 6.0
    assert ledger.shannon_entropy(cipher) > 7.9


def test_checksum_drift_is_detected():
    import hashlib
    good = b"backup payload" * 200
    h = hashlib.sha256(good).hexdigest()
    assert ledger.integrity_probe(good, expected_sha256=h)["checksum_match"] is True
    tampered = ledger.integrity_probe(b"backup payl0ad" * 200, expected_sha256=h)
    assert tampered["checksum_match"] is False
    assert any("CHECKSUM DRIFT" in f for f in tampered["findings"])


def test_ransomware_pattern_flags_entropy_spike():
    """Same size, same name, restore 'succeeds' — contents now ciphertext."""
    import os
    plain = b"user_id,email,balance\n" * 300
    baseline = ledger.shannon_entropy(plain)
    probe = ledger.integrity_probe(os.urandom(len(plain)), baseline_entropy=baseline)
    assert probe["suspicious"]
    assert any("ENTROPY SPIKE" in f for f in probe["findings"])


def test_integrity_not_verified_without_a_checksum():
    """Absence of evidence is not evidence of integrity."""
    probe = ledger.integrity_probe(b"some restored bytes")
    assert probe["integrity_verified"] is False
    assert probe["checksum_match"] is None


def test_checksum_drift_overrides_a_passing_restore(tmp_path, monkeypatch):
    """A restore that returns corrupted data has not proven recoverability —
    it has disproven it. The job's own 'passed' must not win."""
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ev.jsonl")
    ledger.reset()
    import hashlib
    expected = hashlib.sha256(b"the original bytes").hexdigest()
    rec = ledger.record_verified_test(
        "corrupt-01", "full_restore_drill", "passed",
        sample=b"NOT the original bytes", expected_sha256=expected)
    assert rec["outcome"] == "failed"
    assert rec["integrity"]["checksum_match"] is False


def test_verified_restore_records_integrity_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "LEDGER_PATH", tmp_path / "ev.jsonl")
    ledger.reset()
    import hashlib
    payload = b"clean restored database dump" * 50
    rec = ledger.record_verified_test(
        "clean-01", "full_restore_drill", "passed",
        sample=payload, expected_sha256=hashlib.sha256(payload).hexdigest())
    assert rec["outcome"] == "passed"
    assert rec["integrity"]["integrity_verified"] is True
    assert rec["checksum_verified"] is True
