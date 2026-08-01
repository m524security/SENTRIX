import React, { useEffect, useState, useCallback } from 'react';
import { fmtTime } from '../lib';

const sevTag = { p1: 'p1', p2: 'p2', warning: 'warn', info: 'info' };

export default function AuditLog() {
  const [records, setRecords] = useState([]);
  const [loading, setLoading] = useState(true);

  const load = useCallback(() => {
    fetch(`${process.env.REACT_APP_API_URL || 'http://localhost:8000'}/api/audit?limit=80`)
      .then((r) => r.json()).then((d) => { setRecords(Array.isArray(d) ? d : []); setLoading(false); })
      .catch(() => setLoading(false));
  }, []);
  useEffect(() => { load(); const iv = setInterval(load, 12000); return () => clearInterval(iv); }, [load]);

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">Tamper-evident trail</div>
          <h2>Audit log</h2>
        </div>
        <p className="desc">Every agent decision is HMAC-signed and recorded. Records persist locally even if the cloud store is unreachable.</p>
      </div>

      {loading ? <div className="loading"><div className="spinner" />loading audit trail…</div>
        : records.length === 0 ? (
          <div className="empty"><div className="ico">☰</div>No audit records yet. They appear once the agent runs cycles.</div>
        ) : (
          <div className="tbl-wrap">
            <table className="tbl">
              <thead><tr><th>Time</th><th>Event</th><th>Asset</th><th>Severity</th><th>Action</th><th>Reasoning</th><th>Sig</th></tr></thead>
              <tbody>
                {records.map((r, i) => (
                  <tr key={r.log_id || i}>
                    <td className="mono" style={{ fontSize: 11.5, whiteSpace: 'nowrap' }}>{fmtTime(r.timestamp)}</td>
                    <td>{r.event_type || r.type || '—'}</td>
                    <td className="id">{r.asset_id || '—'}</td>
                    <td>{r.severity_level ? <span className={`tag ${sevTag[r.severity_level] || 'info'}`}>{r.severity_level}</span> : '—'}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{r.action_taken || '—'}</td>
                    <td style={{ maxWidth: 360 }}>{r.reasoning_output || r.summary || '—'}</td>
                    <td className="mono" style={{ fontSize: 10.5, color: 'var(--text-faint)' }} title={r.signature}>{r.signature ? `${r.signature.slice(0, 8)}…` : '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
    </div>
  );
}
