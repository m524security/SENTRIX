import React, { useEffect, useState, useCallback } from 'react';
import { api, tierLabel } from '../lib';

export default function Assets() {
  const [datasets, setDatasets] = useState({});
  const [selected, setSelected] = useState('all');
  const [assets, setAssets] = useState([]);
  const [loading, setLoading] = useState(true);
  const [q, setQ] = useState('');

  useEffect(() => { api('/api/datasets').then((d) => { if (!d.error) setDatasets(d); }).catch(() => {}); }, []);

  const load = useCallback(() => {
    setLoading(true);
    api(`/api/assets?dataset=${selected}`).then((d) => { setAssets(d.assets || []); setLoading(false); })
      .catch(() => setLoading(false));
  }, [selected]);

  useEffect(() => { load(); const iv = setInterval(load, 15000); return () => clearInterval(iv); }, [load]);

  const filtered = assets.filter((a) =>
    !q || a.asset_id.toLowerCase().includes(q.toLowerCase()) || (a.source_label || '').toLowerCase().includes(q.toLowerCase()));

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">Inventory</div>
          <h2>Monitored assets</h2>
        </div>
        <p className="desc">Every asset is calibrated from a real LogHub dataset — its failure behaviour reflects that dataset's genuine error rate.</p>
      </div>

      <div className="controls">
        <select className="sel" value={selected} onChange={(e) => setSelected(e.target.value)}>
          <option value="all">All datasets ({assets.length && selected === 'all' ? assets.length : '…'})</option>
          {Object.values(datasets).map((d) => (
            <option key={d.id} value={d.id}>{d.label} · {d.asset_count}</option>
          ))}
        </select>
        <input className="txt" placeholder="Filter by id or source…" value={q} onChange={(e) => setQ(e.target.value)} />
        {selected !== 'all' && datasets[selected] && (
          <span className="note">real error rate: {datasets[selected].real_observed_error_rate_pct}% · {datasets[selected].log_lines_analyzed} log lines analyzed</span>
        )}
      </div>

      {loading ? <div className="loading"><div className="spinner" />loading assets…</div> : (
        <div className="tbl-wrap">
          <table className="tbl">
            <thead><tr><th>Asset ID</th><th>Source</th><th>Tier</th><th>Crit</th><th>RPO target</th><th>Last backup</th><th>Status</th></tr></thead>
            <tbody>
              {filtered.map((a) => {
                const failing = a.consecutive_failures > 0;
                return (
                  <tr key={a.asset_id}>
                    <td className="id">{a.asset_id}</td>
                    <td>{a.source_label}</td>
                    <td><span className="tag tier">{tierLabel(a.tier)}</span></td>
                    <td className="num">{a.criticality_score}</td>
                    <td className="num">{a.rpo_target_hours}h</td>
                    <td className="num">{a.hours_since_last_backup}h ago</td>
                    <td>
                      {failing
                        ? <span className="tag p1">{a.consecutive_failures}× FAIL</span>
                        : <span className="tag none">OK</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
