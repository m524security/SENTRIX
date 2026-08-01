import React, { useEffect, useState } from 'react';
import { api, actionClass, actionLabel, riskColor } from '../lib';

export default function Simulation({ onResult }) {
  const [scenarios, setScenarios] = useState([]);
  const [datasets, setDatasets] = useState({});
  const [mode, setMode] = useState('dataset');
  const [scenarioId, setScenarioId] = useState('');
  const [dataset, setDataset] = useState('all');
  const [useFallback, setUseFallback] = useState(false);
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    api('/api/simulate/scenarios').then((d) => {
      setScenarios(d.scenarios || []);
      if (d.scenarios?.[0]) setScenarioId(d.scenarios[0].id || d.scenarios[0].scenario_id);
    }).catch(() => {});
    api('/api/datasets').then((d) => { if (!d.error) setDatasets(d); }).catch(() => {});
  }, []);

  const run = async () => {
    setRunning(true); setError(null); setResult(null);
    const body = mode === 'scenario'
      ? { scenario_id: scenarioId, use_fallback: useFallback }
      : { dataset, use_fallback: useFallback };
    try {
      const d = await api('/api/simulate/trigger', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
      });
      if (d.error) setError(d.error); else { setResult(d); onResult && onResult(); }
    } catch (e) { setError(String(e)); }
    setRunning(false);
  };

  const decisions = (result?.assessments || []).sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0));

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">What-if testing</div>
          <h2>Run a reasoning cycle</h2>
        </div>
        <p className="desc">Trigger the agent on a curated incident scenario or a whole dataset and inspect exactly how it reasons. Results broadcast to the live feed too.</p>
      </div>

      <div className="card card-pad" style={{ marginBottom: 18 }}>
        <div className="controls" style={{ marginBottom: 0 }}>
          <select className="sel" value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="dataset">By dataset</option>
            <option value="scenario">By incident scenario</option>
          </select>

          {mode === 'scenario' ? (
            <select className="sel" value={scenarioId} onChange={(e) => setScenarioId(e.target.value)} style={{ maxWidth: 340 }}>
              {scenarios.map((s) => {
                const id = s.id || s.scenario_id;
                return <option key={id} value={id}>{id} — {s.name || s.title || 'scenario'}</option>;
              })}
            </select>
          ) : (
            <select className="sel" value={dataset} onChange={(e) => setDataset(e.target.value)}>
              <option value="all">All datasets</option>
              {Object.values(datasets).map((d) => <option key={d.id} value={d.id}>{d.label}</option>)}
            </select>
          )}

          <label className="pill" style={{ cursor: 'pointer' }}>
            <input type="checkbox" checked={useFallback} onChange={(e) => setUseFallback(e.target.checked)} style={{ accentColor: 'var(--accent)' }} />
            force rule engine
          </label>

          <button className="btn btn-primary" onClick={run} disabled={running}>
            {running ? 'Running…' : 'Run cycle ▸'}
          </button>
        </div>
        {mode === 'scenario' && scenarios.find((s) => (s.id || s.scenario_id) === scenarioId)?.description && (
          <p className="note" style={{ marginTop: 12 }}>{scenarios.find((s) => (s.id || s.scenario_id) === scenarioId).description}</p>
        )}
      </div>

      {error && <div className="card card-pad" style={{ borderColor: 'var(--p1)', color: 'var(--p1)', marginBottom: 16 }}>Error: {error}</div>}

      {result && (
        <>
          <div className="metric-row">
            <div className="metric"><div className="m-label">Mode</div><div className="m-value" style={{ fontSize: 22 }}>{result.fallback_mode ? 'Rule engine' : `LLM`}</div><div className="m-foot">{result.fallback_mode ? 'deterministic' : `${result.provider} · ${result.model}`}</div></div>
            <div className={`metric ${result.critical_count > 0 ? 'danger' : 'ok'}`}><div className="m-label">Critical</div><div className={`m-value ${result.critical_count > 0 ? 'danger' : 'ok'}`}>{result.critical_count}</div><div className="m-foot">escalations</div></div>
            <div className="metric ok"><div className="m-label">Healthy</div><div className="m-value ok">{result.healthy_count}</div><div className="m-foot">no action needed</div></div>
            <div className="metric"><div className="m-label">Assessed</div><div className="m-value">{decisions.length}</div><div className="m-foot">assets in cycle</div></div>
          </div>

          <div className="card card-pad" style={{ marginBottom: 16 }}>
            <div className="eyebrow" style={{ marginBottom: 6 }}>Summary</div>
            <div style={{ fontSize: 14, color: 'var(--text)' }}>{result.summary}</div>
          </div>

          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th>Asset</th><th>RPO</th><th>Risk</th><th>Action</th><th>Explanation</th></tr></thead>
              <tbody>
                {decisions.map((d) => (
                  <tr key={d.asset_id}>
                    <td className="id">{d.asset_id}</td>
                    <td className="num">{Math.round(d.rpo_percentage)}%</td>
                    <td className="num" style={{ color: riskColor(d.risk_score) }}>{Math.round(d.risk_score)}</td>
                    <td><span className={`tag ${actionClass(d.action)}`}>{actionLabel(d.action)}</span></td>
                    <td>{d.explanation}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}
