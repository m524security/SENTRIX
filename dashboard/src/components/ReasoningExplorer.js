import React, { useState } from 'react';
import { actionClass, actionLabel, riskColor, tierLabel, fmtTime } from '../lib';

export default function ReasoningExplorer({ cycles = [] }) {
  const [selectedId, setSelectedId] = useState(null);
  const [filter, setFilter] = useState('all');

  const cycle = cycles.find((c) => c.cycle_id === selectedId) || cycles[0];
  const decisions = (cycle?.decisions || []).filter((d) => {
    if (filter === 'all') return true;
    if (filter === 'escalations') return d.action?.includes('ESCALATE');
    if (filter === 'actions') return d.action && d.action !== 'NONE';
    return true;
  }).sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0));

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">Decision transparency</div>
          <h2>Reasoning explorer</h2>
        </div>
        <p className="desc">Every assessment the agent made, with its risk math and a plain-English rationale. This is the agent showing its work.</p>
      </div>

      <div className="controls">
        <select className="sel" value={cycle?.cycle_id || ''} onChange={(e) => setSelectedId(e.target.value)}>
          {cycles.length === 0 && <option>no cycles yet</option>}
          {cycles.map((c) => (
            <option key={c.cycle_id} value={c.cycle_id}>
              {c.cycle_id} · {fmtTime(c.timestamp)} · {c.fallback_mode ? 'rule' : (c.provider || 'llm')}
            </option>
          ))}
        </select>
        <select className="sel" value={filter} onChange={(e) => setFilter(e.target.value)}>
          <option value="all">All assessments</option>
          <option value="actions">Actions only</option>
          <option value="escalations">Escalations only</option>
        </select>
        {cycle && <span className="note">{cycle.summary}</span>}
      </div>

      {!cycle ? (
        <div className="empty"><div className="ico">◉</div>No reasoning cycles recorded yet. Run a simulation or start the agent.</div>
      ) : decisions.length === 0 ? (
        <div className="empty"><div className="ico">✓</div>No assessments match this filter in cycle {cycle.cycle_id}.</div>
      ) : (
        decisions.map((d) => (
          <div className="card card-pad" key={d.asset_id} style={{ marginBottom: 12 }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: 14, flexWrap: 'wrap' }}>
              <div style={{ minWidth: 0 }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 6, flexWrap: 'wrap' }}>
                  <span className="mono" style={{ fontSize: 14, color: 'var(--text)' }}>{d.asset_id}</span>
                  <span className="tag tier">{tierLabel(d.tier)}</span>
                  <span className={`tag ${actionClass(d.action)}`}>{actionLabel(d.action)}</span>
                  <span className="tag tier">{d.mode === 'rule' ? 'rule' : 'llm'}</span>
                </div>
                <div style={{ fontSize: 13.5, color: 'var(--text-dim)', lineHeight: 1.55 }}>{d.explanation}</div>
                {d.evidence && <div className="feed-evidence" style={{ marginTop: 10 }}>log evidence: {d.evidence}</div>}
              </div>
              <div style={{ textAlign: 'right', flexShrink: 0 }}>
                <div className="mono" style={{ fontSize: 26, fontWeight: 600, color: riskColor(d.risk_score) }}>{Math.round(d.risk_score)}</div>
                <div className="note">risk score</div>
                <div className="mono" style={{ fontSize: 12, color: 'var(--text-dim)', marginTop: 6 }}>{Math.round(d.rpo_percentage)}% RPO</div>
              </div>
            </div>
            <div className="riskbar" style={{ marginTop: 12 }}>
              <span style={{ width: `${Math.min(100, d.risk_score / 6)}%`, background: riskColor(d.risk_score) }} />
            </div>
          </div>
        ))
      )}
    </div>
  );
}
