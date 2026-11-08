import React, { useEffect, useState, useCallback } from 'react';
import { api, tierLabel } from '../lib';

export default function RestoreTests() {
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    api('/api/restore-tests').then((d) => { setRecords(Array.isArray(d) ? d : []); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);
  useEffect(() => { load(); const iv = setInterval(load, 15000); return () => clearInterval(iv); }, [load]);

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">Recovery verification</div>
          <h2>Restore drill backlog</h2>
        </div>
        <p className="desc">A backup you never restore is a backup you don't have. These assets are overdue for a recovery drill, most urgent first.</p>
      </div>

      {loading ? <div className="loading"><div className="spinner" />checking restore cadence…</div>
        : records.length === 0 ? (
          <div className="empty"><div className="ico">✓</div>No overdue restore drills. Every asset's recovery has been verified within cadence.</div>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th>Asset</th><th>Tier</th><th>Cadence</th><th>Overdue by</th><th>Status</th></tr></thead>
              <tbody>
                {records.map((r) => (
                  <tr key={r.asset_id}>
                    <td className="id">{r.asset_id}</td>
                    <td><span className="tag tier">{tierLabel(r.tier)}</span></td>
                    <td className="num">every {r.cadence_days}d</td>
                    <td className="num" style={{ color: r.days_overdue > 7 ? 'var(--p1)' : 'var(--warn)' }}>{r.days_overdue}d</td>
                    <td><span className={`tag ${r.days_overdue > 7 ? 'p1' : 'warn'}`}>OVERDUE</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}
