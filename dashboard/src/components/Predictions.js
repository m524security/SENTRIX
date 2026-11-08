import React, { useEffect, useState, useCallback } from 'react';
import { api, tierLabel } from '../lib';

const riskTag = { high: 'p1', medium: 'warn', low: 'none', unknown: 'tier' };

export default function Predictions() {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    api('/api/predictions').then((d) => { setData(d); setLoading(false); }).catch(() => setLoading(false));
  }, []);
  useEffect(() => { load(); const iv = setInterval(load, 15000); return () => clearInterval(iv); }, [load]);

  const atRisk = data?.at_risk || [];

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">Predictive engine</div>
          <h2>RPO breach forecast</h2>
        </div>
        <p className="desc">SENTRIX projects each asset's failure trend forward to warn of breaches before they occur — this is the shift from reactive monitoring to proactive recovery.</p>
      </div>

      <div className="metric-row">
        <div className={`metric ${(data?.high ?? 0) > 0 ? 'danger' : 'ok'}`}>
          <div className="m-label">High-risk forecasts</div>
          <div className={`m-value ${(data?.high ?? 0) > 0 ? 'danger' : 'ok'}`}>{data?.high ?? '—'}</div>
          <div className="m-foot">breach predicted soon</div>
        </div>
        <div className={`metric ${(data?.medium ?? 0) > 0 ? 'warn' : 'ok'}`}>
          <div className="m-label">Medium-risk forecasts</div>
          <div className={`m-value ${(data?.medium ?? 0) > 0 ? 'warn' : 'ok'}`}>{data?.medium ?? '—'}</div>
          <div className="m-foot">elevated, monitor closely</div>
        </div>
        <div className="metric">
          <div className="m-label">Assets analyzed</div>
          <div className="m-value">{data?.count ?? '—'}</div>
          <div className="m-foot">trend projected per asset</div>
        </div>
      </div>

      {loading ? <div className="loading"><div className="spinner" />running forecast…</div>
        : atRisk.length === 0 ? (
          <div className="empty"><div className="ico">◷</div>No assets are forecast to breach RPO. Fleet trending stable.</div>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th>Asset</th><th>Tier</th><th>Forecast</th><th>ETA</th><th>Reasoning</th></tr></thead>
              <tbody>
                {atRisk.map((f) => (
                  <tr key={f.asset_id}>
                    <td className="id">{f.asset_id}</td>
                    <td><span className="tag tier">{tierLabel(f.tier)}</span></td>
                    <td><span className={`tag ${riskTag[f.risk]}`}>{f.risk.toUpperCase()}</span></td>
                    <td className="num">{f.predicted_breach_hours ? `~${f.predicted_breach_hours}h` : '—'}</td>
                    <td>{f.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}
