// =========================================================================== //
// SENTRIX — Recovery Readiness Dashboard (PS284 deliverable)
// =========================================================================== //
// The framing difference from every backup dashboard on the market:
// we do not report whether backups SUCCEEDED. We report whether recovery is
// PROVEN — and we show the queue of tests the agent scheduled to close the gap.
//
// Three panels:
//   1. Fleet confidence + band distribution  — "how much of this is provable?"
//   2. Blind spots with provenance           — "why don't you believe it?"
//   3. The agent's evidence-acquisition plan — "what is it doing about it?"
// =========================================================================== //
import React, { useEffect, useState, useCallback } from 'react';
import { ShieldCheck, ShieldAlert, ShieldQuestion, Clock, GitCommit, Play, AlertTriangle } from 'lucide-react';
import { api, API_BASE } from '../lib';

const BAND_COLOR = {
  proven: '#22c55e',
  probable: '#84cc16',
  unproven: '#f59e0b',
  blind_spot: '#ef4444',
};
const BAND_ICON = {
  proven: ShieldCheck,
  probable: ShieldCheck,
  unproven: ShieldQuestion,
  blind_spot: ShieldAlert,
};

function pct(n) { return `${(n ?? 0).toFixed(1)}%`; }

// --- Decay curve: the chart that explains the whole model ------------------ //
function DecayCurve({ curve }) {
  if (!curve?.points?.length) return null;
  const pts = curve.points;
  const W = 460, H = 130, PAD = 4;
  const xs = pts.map((p) => p.days_from_now);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const sx = (d) => PAD + ((d - minX) / (maxX - minX || 1)) * (W - PAD * 2);
  const sy = (c) => H - PAD - (c / 100) * (H - PAD * 2);
  const path = pts.map((p, i) => `${i ? 'L' : 'M'}${sx(p.days_from_now).toFixed(1)},${sy(p.confidence_pct).toFixed(1)}`).join(' ');
  const todayX = sx(0);
  const threshY = sy(curve.unproven_threshold_pct ?? 35);

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="decay-curve" role="img"
         aria-label="Recovery confidence decaying over time since the last restore test">
      <line x1={PAD} y1={threshY} x2={W - PAD} y2={threshY}
            stroke="#ef4444" strokeWidth="1" strokeDasharray="4 4" opacity="0.6" />
      <text x={W - PAD} y={threshY - 4} textAnchor="end" fontSize="9" fill="#ef4444">
        unproven threshold
      </text>
      <path d={path} fill="none" stroke="#6366f1" strokeWidth="2" />
      <line x1={todayX} y1={PAD} x2={todayX} y2={H - PAD} stroke="#94a3b8" strokeWidth="1" />
      <text x={todayX + 4} y={PAD + 9} fontSize="9" fill="#94a3b8">today</text>
      <text x={PAD} y={H - PAD + 0} fontSize="9" fill="#64748b">last test</text>
      <text x={W - PAD} y={H - PAD} textAnchor="end" fontSize="9" fill="#64748b">
        +{Math.round(maxX)}d if untested
      </text>
    </svg>
  );
}

// --- One blind-spot card, with full provenance ---------------------------- //
function AssetCard({ asset, onTest, testing }) {
  const [detail, setDetail] = useState(null);
  const [open, setOpen] = useState(false);
  const Icon = BAND_ICON[asset.band] || ShieldQuestion;

  const expand = async () => {
    setOpen(!open);
    if (!detail) {
      try { setDetail(await api(`/api/recovery/asset/${asset.asset_id}`)); }
      catch (e) { setDetail({ error: e.message }); }
    }
  };

  const p = detail?.evidence_provenance;
  return (
    <div className="readiness-card">
      <div className="readiness-card-head" onClick={expand}>
        <Icon size={16} color={BAND_COLOR[asset.band]} />
        <div className="readiness-card-title">
          <strong>{asset.asset_name}</strong>
          <span className="tier-chip">T{asset.tier}</span>
        </div>
        <div className="readiness-score" style={{ color: BAND_COLOR[asset.band] }}>
          {pct(asset.confidence_pct)}
          {asset.confidence_interval != null &&
            <span className="ci"> ±{(asset.confidence_interval * 100).toFixed(0)}</span>}
        </div>
      </div>

      {/* The gaps ARE the product: why the agent doesn't believe this is recoverable */}
      <ul className="gap-list">
        {(asset.gaps || []).map((g, i) => <li key={i}>{g}</li>)}
      </ul>

      {open && detail && !detail.error && (
        <div className="readiness-detail">
          <DecayCurve curve={detail.decay_curve} />
          <div className="prov-grid">
            <div><Clock size={11} /> Last proven restore</div>
            <div>{p?.test_type === 'none' ? 'never' : `${Math.round(p?.days_since_test)}d ago (${p?.test_type?.replace(/_/g, ' ')})`}</div>
            <div>Evidence decayed to</div>
            <div>{((p?.decay_factor ?? 0) * 100).toFixed(1)}% of original strength
              <span className="muted"> (tier-{asset.tier} half-life {p?.evidence_half_life_days}d)</span></div>
            <div><GitCommit size={11} /> Config drift since</div>
            <div>{p?.config_changes_since ?? 0} changes</div>
            {p?.rto_proven != null && (<>
              <div>RTO target met</div>
              <div style={{ color: p.rto_proven ? '#22c55e' : '#ef4444' }}>
                {p.rto_proven ? 'yes' : 'no — last drill missed target'}</div>
            </>)}
          </div>
          <div className="contrib-bar">
            {Object.entries(detail.contributions || {}).map(([k, v]) => (
              <div key={k} className="contrib-row">
                <span>{k.replace(/_/g, ' ')}</span>
                <span className={v < 0 ? 'neg' : ''}>{v >= 0 ? '+' : ''}{(v * 100).toFixed(1)}</span>
              </div>
            ))}
          </div>
          <button className="btn-test" disabled={testing === asset.asset_id}
                  onClick={() => onTest(asset.asset_id, detail.next_test?.test_type)}>
            <Play size={11} /> {testing === asset.asset_id ? 'Recording…' : 'Simulate passing restore test'}
          </button>
        </div>
      )}
    </div>
  );
}

export default function RecoveryReadiness() {
  const [readiness, setReadiness] = useState(null);
  const [plan, setPlan] = useState(null);
  const [budget, setBudget] = useState(24);
  const [err, setErr] = useState(null);
  const [testing, setTesting] = useState(null);
  const [token, setToken] = useState('');

  const load = useCallback(async () => {
    try {
      setErr(null);
      const [r, p] = await Promise.all([
        api('/api/recovery/readiness'),
        api(`/api/recovery/plan?budget_hours=${budget}`),
      ]);
      setReadiness(r); setPlan(p);
    } catch (e) { setErr(e.message); }
  }, [budget]);

  useEffect(() => { load(); }, [load]);

  // Closing the loop from the UI: record a passing test, watch confidence jump.
  const recordTest = async (assetId, testType) => {
    setTesting(assetId);
    try {
      const res = await fetch(`${API_BASE}/api/recovery/evidence`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json',
                   ...(token ? { Authorization: `Bearer ${token}` } : {}) },
        body: JSON.stringify({
          asset_id: assetId, test_type: testType || 'full_restore_drill',
          outcome: 'passed', rto_actual_seconds: 900, checksum_verified: true,
          notes: 'Recorded from readiness dashboard',
        }),
      });
      const j = await res.json();
      if (!j.ok) throw new Error(j.error || 'write rejected');
      await load();
    } catch (e) { setErr(`Evidence write failed: ${e.message}`); }
    finally { setTesting(null); }
  };

  if (err && !readiness) return <div className="error-box">Failed to load readiness: {err}</div>;
  if (!readiness) return <div className="loading">Scoring recovery confidence…</div>;

  const bands = readiness.bands || {};
  const total = readiness.asset_count || 1;

  return (
    <div className="readiness">
      <div className="readiness-hero">
        <div>
          <div className="hero-label">Fleet recovery confidence</div>
          <div className="hero-value">{pct(readiness.fleet_confidence_pct)}</div>
          <div className="hero-sub">
            criticality-weighted · {readiness.blind_spot_count} of {total} assets not provably recoverable
          </div>
        </div>
        <div className="band-bars">
          {['proven', 'probable', 'unproven', 'blind_spot'].map((b) => (
            <div key={b} className="band-row">
              <span className="band-name">{b.replace('_', ' ')}</span>
              <div className="band-track">
                <div className="band-fill" style={{
                  width: `${((bands[b] || 0) / total) * 100}%`,
                  background: BAND_COLOR[b],
                }} />
              </div>
              <span className="band-count">{bands[b] || 0}</span>
            </div>
          ))}
        </div>
      </div>

      {/* The agentic panel — what the agent decided to DO about its uncertainty */}
      <div className="plan-panel">
        <div className="plan-head">
          <strong>Evidence acquisition plan</strong>
          <div className="budget-ctl">
            <label>Test budget</label>
            <input type="range" min="2" max="80" step="2" value={budget}
                   onChange={(e) => setBudget(Number(e.target.value))} />
            <span>{budget}h</span>
          </div>
        </div>
        <p className="plan-summary">{plan?.summary}</p>

        {plan?.unfunded_critical_count > 0 && (
          <div className="unfunded-warn">
            <AlertTriangle size={13} />
            {plan.unfunded_critical_count} critical tier-1/2 asset(s) remain unproven and unfunded.
            Raise the budget to cover them.
          </div>
        )}

        <div className="plan-list">
          {(plan?.scheduled || []).slice(0, 8).map((s) => (
            <div key={s.asset_id} className={`plan-item ${s.tranche}`}>
              <span className={`tranche-chip ${s.tranche}`}>{s.tranche}</span>
              <span className="plan-asset">{s.asset_name}</span>
              <span className="plan-test">{s.test_type.replace(/_/g, ' ')}</span>
              <span className="plan-cost">{s.cost_hours}h</span>
              <span className="plan-gain">+{(s.confidence_gain * 100).toFixed(1)} pts</span>
            </div>
          ))}
        </div>
      </div>

      <div className="token-row">
        <input type="password" placeholder="Write token (to record evidence)"
               value={token} onChange={(e) => setToken(e.target.value)} />
        {err && <span className="inline-err">{err}</span>}
      </div>

      <div className="blind-spots">
        <h3>Not provably recoverable</h3>
        <p className="section-sub">
          Backups may be green. These are the assets you have not proven you can restore.
        </p>
        {(readiness.blind_spots || []).map((a) => (
          <AssetCard key={a.asset_id} asset={a} onTest={recordTest} testing={testing} />
        ))}
      </div>
    </div>
  );
}
