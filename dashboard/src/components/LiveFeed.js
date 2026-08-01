import React from 'react';
import { actionClass, actionLabel, fmtTime } from '../lib';

const railColor = { p1: 'var(--p1)', p2: 'var(--p2)', warn: 'var(--warn)', none: 'var(--ok)', info: 'var(--accent)' };

export default function LiveFeed({ cycles = [], ws }) {
  // Flatten the most recent cycles into a stream of notable events (anything not NONE),
  // newest first, plus a cycle marker.
  const events = [];
  cycles.slice(0, 10).forEach((cycle) => {
    events.push({ kind: 'cycle', cycle });
    (cycle.decisions || []).filter((d) => d.action && d.action !== 'NONE')
      .sort((a, b) => (b.risk_score || 0) - (a.risk_score || 0))
      .slice(0, 5)
      .forEach((d) => events.push({ kind: 'decision', cycle, decision: d }));
  });

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">Real-time stream</div>
          <h2>Live agent feed</h2>
        </div>
        <p className="desc">
          {ws ? 'Connected — new cycles appear here the moment the agent publishes them.'
              : 'Offline — start the API and agent to stream live cycles. Showing the latest recorded cycles.'}
        </p>
      </div>

      {events.length === 0 ? (
        <div className="empty"><div className="ico">⚡</div>No cycles yet. Start the agent loop or run a simulation to populate the feed.</div>
      ) : events.map((ev, i) => {
        if (ev.kind === 'cycle') {
          const c = ev.cycle;
          return (
            <div className="feed-item" key={`${c.cycle_id}-head-${i}`}>
              <div className="feed-rail" style={{ background: c.critical_count > 0 ? 'var(--p1)' : 'var(--ok)' }} />
              <div className="feed-body">
                <div className="feed-meta">
                  <span className="tag info">CYCLE {c.cycle_id}</span>
                  <span className="tag tier">{c.fallback_mode ? 'RULE ENGINE' : `LLM · ${c.provider || 'llm'}`}</span>
                  <span className="feed-time">{fmtTime(c.timestamp)}</span>
                </div>
                <div className="feed-summary">{c.summary}</div>
                <div className="feed-detail">
                  {c.asset_count} assets · {c.critical_count} critical · {c.healthy_count} healthy · {c.action_count ?? 0} actions
                </div>
              </div>
            </div>
          );
        }
        const d = ev.decision;
        const cls = actionClass(d.action);
        return (
          <div className="feed-item" key={`${ev.cycle.cycle_id}-${d.asset_id}-${i}`} style={{ marginLeft: 22 }}>
            <div className="feed-rail" style={{ background: railColor[cls] }} />
            <div className="feed-body">
              <div className="feed-meta">
                <span className={`tag ${cls}`}>{actionLabel(d.action)}</span>
                <span className="mono" style={{ fontSize: 12, color: 'var(--text)' }}>{d.asset_id}</span>
                <span className="feed-time">risk {Math.round(d.risk_score)} · {Math.round(d.rpo_percentage)}% RPO</span>
              </div>
              <div className="feed-detail">{d.explanation}</div>
              {d.evidence && <div className="feed-evidence">log: {d.evidence}</div>}
            </div>
          </div>
        );
      })}
    </div>
  );
}
