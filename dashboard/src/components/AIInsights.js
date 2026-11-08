import React, { useEffect, useState, useCallback } from 'react';
import { api, riskColor } from '../lib';
import {
  Brain, Activity, TrendingUp, Eye, Database,
  Cpu, Zap, CheckCircle, XCircle, Layers, Search,
} from 'lucide-react';

const METHOD_LABELS = {
  zero_shot: 'Zero-Shot (BART-MNLI)',
  zero_shot_sentiment: 'Zero-Shot + Sentiment',
  'zero_shot+sentiment': 'Zero-Shot + Sentiment',
  keyword_fallback: 'Keyword Rules',
  lstm: 'LSTM Autoencoder',
  statistical: 'Statistical (Z-score/IQR)',
  fallback: 'Rule-based Fallback',
  transformer: 'Transformer Self-Attention',
  exp_smoothing: 'Exponential Smoothing',
  linear: 'Linear Extrapolation',
  unavailable: 'Not Available',
};

function methodLabel(m) {
  return METHOD_LABELS[m] || m || 'Unknown';
}

function ScoreBar({ score, color }) {
  const pct = Math.min(100, Math.round((score || 0) * 100));
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div className="riskbar" style={{ flex: 1 }}>
        <span style={{ width: `${pct}%`, background: color || (pct > 60 ? 'var(--p1)' : pct > 30 ? 'var(--warn)' : 'var(--ok)') }} />
      </div>
      <span className="mono" style={{ fontSize: 10, width: 34, textAlign: 'right', color: 'var(--text-dim)' }}>{pct}%</span>
    </div>
  );
}

function StatusChip({ available, label }) {
  return (
    <span className={`pill ${available ? 'live' : 'offline'}`} style={{ fontSize: 10, padding: '3px 10px' }}>
      {available ? <CheckCircle size={10} /> : <XCircle size={10} />}
      {label && <span style={{ marginLeft: 2 }}>{label}</span>}
    </span>
  );
}

function GlassCard({ title, icon, iconColor, status, children, colSpan }) {
  return (
    <div className="card" style={{ gridColumn: colSpan === 2 ? 'span 2' : undefined }}>
      <div className="card-head">
        <h3 style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13 }}>
          {icon && <span style={{ color: iconColor || 'var(--accent)' }}>{icon}</span>}
          {title}
        </h3>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {status}
        </div>
      </div>
      <div className="card-pad" style={{ padding: '14px 18px' }}>{children}</div>
    </div>
  );
}

function BigNumber({ label, value, color, sub }) {
  return (
    <div>
      <div className="eyebrow">{label}</div>
      <div className="mono" style={{ fontSize: 30, fontWeight: 700, color: color || 'var(--text)', marginTop: 4 }}>
        {value}
      </div>
      {sub && <div style={{ fontSize: 10, color: 'var(--text-faint)', marginTop: 2, fontFamily: 'var(--mono)' }}>{sub}</div>}
    </div>
  );
}

function LogClassifier() {
  const [logLine, setLogLine] = useState('');
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);

  const classify = async () => {
    if (!logLine.trim()) return;
    setLoading(true);
    try {
      const res = await api('/api/ai-insights/classify', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ log_line: logLine, find_similar: true }),
      });
      setResult(res);
    } catch (e) {
      setResult({ ok: false, error: e.message });
    } finally {
      setLoading(false);
    }
  };

  return (
    <div>
      <div className="eyebrow" style={{ marginBottom: 10 }}>Classify a log line</div>
      <div style={{ display: 'flex', gap: 8 }}>
        <div style={{ flex: 1, position: 'relative' }}>
          <Search size={14} style={{ position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)', color: 'var(--text-faint)' }} />
          <input
            type="text" value={logLine}
            onChange={e => setLogLine(e.target.value)}
            onKeyDown={e => e.key === 'Enter' && classify()}
            placeholder="Paste any log line — e.g. ERROR: backup agent timeout..."
            className="log-input"
            style={{ paddingLeft: 34 }}
          />
        </div>
        <button onClick={classify} disabled={loading || !logLine.trim()} style={{
          background: 'linear-gradient(135deg, var(--accent), #818cf8)',
          color: '#030712', border: 'none', borderRadius: 'var(--radius-sm)',
          padding: '0 18px', cursor: 'pointer', fontSize: 12, fontWeight: 700,
          opacity: loading ? 0.5 : 1, transition: 'all 0.2s',
        }}>
          {loading ? '...' : 'Classify'}
        </button>
      </div>

      {result && (
        <div style={{ marginTop: 14, background: 'var(--glass)', borderRadius: 'var(--radius-sm)', padding: 16, fontSize: 12, border: '1px solid var(--glass-border)', backdropFilter: 'blur(8px)' }}>
          {result.ok === false ? (
            <span style={{ color: 'var(--p1)' }}>Error: {result.error}</span>
          ) : (
            <>
              <div style={{ display: 'flex', gap: 20, flexWrap: 'wrap', marginBottom: 12 }}>
                <div>
                  <div className="eyebrow">Severity</div>
                  <span className={`tag ${result.label === 'critical failure' ? 'p1' : result.label === 'warning' ? 'warn' : 'none'}`} style={{ marginTop: 6, display: 'inline-block' }}>
                    {result.label}
                  </span>
                </div>
                <div>
                  <div className="eyebrow">Anomaly</div>
                  <span className="mono" style={{ fontSize: 22, fontWeight: 700, color: riskColor((result.anomaly_score || 0) * 600) }}>
                    {Math.round((result.anomaly_score || 0) * 100)}%
                  </span>
                </div>
                <div>
                  <div className="eyebrow">Method</div>
                  <span className="mono" style={{ color: 'var(--text-dim)', fontSize: 11, marginTop: 4, display: 'block' }}>{methodLabel(result.method)}</span>
                </div>
              </div>
              {result.similar_incidents?.length > 0 && (
                <div>
                  <div className="eyebrow" style={{ marginBottom: 8 }}>Similar fleet incidents</div>
                  {result.similar_incidents.map((s, i) => (
                    <div key={i} style={{ marginBottom: 5, display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                      <span className="mono" style={{ color: 'var(--text-faint)', minWidth: 36, fontSize: 10 }}>{Math.round((s.similarity || 0) * 100)}%</span>
                      <span style={{ color: 'var(--text-dim)', fontFamily: 'var(--mono)', fontSize: 10.5, wordBreak: 'break-all' }}>{s.line}</span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function AIInsights() {
  const [mlStatus, setMlStatus] = useState(null);
  const [insights, setInsights] = useState(null);
  const [anomaly, setAnomaly] = useState(null);
  const [mlPreds, setMlPreds] = useState(null);
  const [visual, setVisual] = useState(null);
  const [hfDatasets, setHfDatasets] = useState(null);
  const [loading, setLoading] = useState(true);
  const [activeSection, setActiveSection] = useState('transformer');

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [status, ins, anom, preds, vis, hf] = await Promise.allSettled([
        api('/api/ml-status'),
        api('/api/ai-insights'),
        api('/api/anomaly-scores'),
        api('/api/ml-predictions'),
        api('/api/visual-analysis'),
        api('/api/hf-datasets'),
      ]);
      if (status.status === 'fulfilled') setMlStatus(status.value);
      if (ins.status === 'fulfilled') setInsights(ins.value);
      if (anom.status === 'fulfilled') setAnomaly(anom.value);
      if (preds.status === 'fulfilled') setMlPreds(preds.value);
      if (vis.status === 'fulfilled') setVisual(vis.value);
      if (hf.status === 'fulfilled') setHfDatasets(hf.value);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { load(); const iv = setInterval(load, 30000); return () => clearInterval(iv); }, [load]);

  const te = mlStatus?.transformer_engine || {};
  const ad = mlStatus?.anomaly_detector || {};
  const ym = mlStatus?.yolo_monitor || {};
  const ts = mlStatus?.time_series_forecaster || {};

  const SECTIONS = [
    { id: 'transformer', label: 'Transformer', icon: <Brain size={14} /> },
    { id: 'anomaly', label: 'Anomaly', icon: <Activity size={14} /> },
    { id: 'forecast', label: 'Forecast', icon: <TrendingUp size={14} /> },
    { id: 'vision', label: 'Vision', icon: <Eye size={14} /> },
    { id: 'datasets', label: 'Datasets', icon: <Database size={14} /> },
  ];

  return (
    <div>
      <div className="section-head">
        <div>
          <div className="eyebrow">AI Engine</div>
          <h2>Deep Learning & ML</h2>
        </div>
        <p className="desc">
          HuggingFace Transformers &middot; PyTorch LSTM &middot; YOLO Vision &middot; Time-Series ML
        </p>
      </div>

      {/* Module status strip */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 22 }}>
        <StatusChip available={te.transformers_available} label="Transformers" />
        <StatusChip available={te.torch_available} label="PyTorch" />
        <StatusChip available={te.sentence_transformers_available} label="Embeddings" />
        <StatusChip available={ad.mode === 'lstm'} label="LSTM" />
        <StatusChip available={ym.yolo_available} label="YOLO" />
        <StatusChip available={ts.statsmodels_available} label="Statsmodels" />
        <StatusChip available={te.cuda_available} label="CUDA" />
        {loading && <span className="pill" style={{ fontSize: 10, padding: '2px 8px', opacity: 0.5 }}>refreshing...</span>}
      </div>

      {/* Section nav — pill style */}
      <div style={{
        display: 'flex', gap: 6, marginBottom: 24, padding: '4px',
        background: 'var(--glass)', borderRadius: 'var(--radius-sm)',
        border: '1px solid var(--glass-border)', width: 'fit-content',
      }}>
        {SECTIONS.map(s => (
          <button key={s.id} onClick={() => setActiveSection(s.id)} style={{
            display: 'flex', alignItems: 'center', gap: 6,
            background: activeSection === s.id ? 'var(--accent-dim)' : 'transparent',
            border: activeSection === s.id ? '1px solid rgba(56,189,248,0.2)' : '1px solid transparent',
            padding: '7px 14px', cursor: 'pointer', borderRadius: 'var(--radius-xs)',
            fontSize: 12, fontWeight: 600, fontFamily: 'var(--sans)',
            color: activeSection === s.id ? 'var(--accent)' : 'var(--text-faint)',
            transition: 'all 0.2s',
          }}>
            {s.icon} {s.label}
          </button>
        ))}
      </div>

      {/* ── TRANSFORMER ANALYSIS ── */}
      {activeSection === 'transformer' && (
        <div className="grid-2" style={{ gap: 14 }}>
          <GlassCard
            title="Fleet Log Analysis"
            icon={<Brain size={14} />}
            iconColor="var(--accent)"
            status={<StatusChip available={!!insights?.analyzed} label={insights ? `${insights.analyzed} analyzed` : 'loading'} />}
          >
            {!insights ? (
              <div className="loading"><div className="spinner" />running transformer analysis...</div>
            ) : insights.ok === false ? (
              <div style={{ color: 'var(--text-dim)', fontSize: 12 }}>
                <div style={{ marginBottom: 8 }}>Transformer models not loaded yet.</div>
                <code className="mono" style={{ fontSize: 10, color: 'var(--text-faint)' }}>pip install transformers torch sentence-transformers</code>
              </div>
            ) : (
              <>
                <div style={{ display: 'flex', gap: 24, marginBottom: 16 }}>
                  <BigNumber label="Fleet anomaly" value={`${Math.round((insights.fleet_anomaly_score || 0) * 100)}%`} color={riskColor((insights.fleet_anomaly_score || 0) * 800)} />
                  <BigNumber label="Critical signals" value={insights.critical_signals?.length ?? 0} color={insights.critical_signals?.length > 0 ? 'var(--p1)' : 'var(--ok)'} />
                  <BigNumber label="Method" value={methodLabel(insights.method)} color="var(--text-dim)" />
                </div>

                {insights.critical_signals?.length > 0 && (
                  <>
                    <div className="eyebrow" style={{ marginBottom: 8 }}>Top Critical Signals</div>
                    <div className="tbl-wrap" style={{ border: 'none' }}>
                      <table className="tbl" style={{ fontSize: 11 }}>
                        <thead><tr><th>Asset</th><th>T</th><th>Anomaly</th><th>Severity</th><th>Evidence</th></tr></thead>
                        <tbody>
                          {insights.critical_signals.slice(0, 8).map(s => (
                            <tr key={s.asset_id}>
                              <td className="id">{s.asset_id}</td>
                              <td><span className="tag tier">T{s.tier}</span></td>
                              <td><ScoreBar score={s.anomaly_score} /></td>
                              <td><span className={`tag ${s.severity_label === 'critical failure' ? 'p1' : s.severity_label === 'warning' ? 'warn' : 'none'}`}>{s.severity_label}</span></td>
                              <td style={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--text-faint)', fontFamily: 'var(--mono)', fontSize: 10 }}>{s.evidence}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </>
                )}
              </>
            )}
          </GlassCard>

          <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
            <GlassCard title="Log Classifier" icon={<Search size={14} />} iconColor="var(--violet)" status={<StatusChip available={te.transformers_available} label={te.transformers_available ? 'transformer' : 'keyword'} />}>
              <LogClassifier />
            </GlassCard>

            <GlassCard title="Model Status" icon={<Layers size={14} />} iconColor="var(--text-dim)" status={null}>
              <div style={{ fontSize: 12 }}>
                {[
                  ['Sentiment / Anomaly', 'distilbert-base-uncased-finetuned-sst-2', te.models_ready?.sentiment],
                  ['Zero-Shot Severity', 'facebook/bart-large-mnli', te.models_ready?.zero_shot],
                  ['Semantic Embeddings', 'all-MiniLM-L6-v2', te.models_ready?.embedder],
                ].map(([name, model, ready]) => (
                  <div key={name} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 10, paddingBottom: 10, borderBottom: '1px solid var(--line)' }}>
                    <div>
                      <div style={{ color: 'var(--text)', fontWeight: 600, fontSize: 12 }}>{name}</div>
                      <div className="mono" style={{ fontSize: 9.5, color: 'var(--text-faint)' }}>{model}</div>
                    </div>
                    <StatusChip available={ready} label={ready ? 'loaded' : 'pending'} />
                  </div>
                ))}
              </div>
            </GlassCard>
          </div>
        </div>
      )}

      {/* ── ANOMALY DETECTION ── */}
      {activeSection === 'anomaly' && (
        <div className="grid-2" style={{ gap: 14 }}>
          <GlassCard title="LSTM Anomaly Detection" icon={<Activity size={14} />} iconColor="var(--p1)"
            status={<StatusChip available={ad.mode === 'lstm'} label={ad.mode === 'lstm' ? 'LSTM active' : ad.mode || 'loading'} />}>
            {!anomaly ? (
              <div className="loading"><div className="spinner" />running anomaly detection...</div>
            ) : anomaly.ok === false ? (
              <div style={{ color: 'var(--text-dim)', fontSize: 12 }}>{anomaly.error}</div>
            ) : (
              <>
                <div style={{ display: 'flex', gap: 24, marginBottom: 16 }}>
                  <BigNumber label="Anomalous" value={anomaly.anomalous_count} color={anomaly.anomalous_count > 0 ? 'var(--p1)' : 'var(--ok)'} />
                  <BigNumber label="Fleet rate" value={`${Math.round((anomaly.fleet_anomaly_rate || 0) * 100)}%`} />
                  <BigNumber label="Method" value={methodLabel(anomaly.method)} color="var(--text-dim)" />
                </div>
                <div className="eyebrow" style={{ marginBottom: 8 }}>Top Anomalies</div>
                <div className="tbl-wrap" style={{ border: 'none' }}>
                  <table className="tbl" style={{ fontSize: 11 }}>
                    <thead><tr><th>Asset</th><th>T</th><th>Score</th><th>Latest</th><th>Baseline</th></tr></thead>
                    <tbody>
                      {(anomaly.top_anomalies || []).slice(0, 10).map(a => (
                        <tr key={a.asset_id} style={{ opacity: a.is_anomalous ? 1 : 0.5 }}>
                          <td className="id">{a.asset_id}</td>
                          <td><span className="tag tier">T{a.tier}</span></td>
                          <td style={{ minWidth: 100 }}><ScoreBar score={a.anomaly_score} /></td>
                          <td className="num">{a.latest_value}h</td>
                          <td className="num" style={{ color: 'var(--text-faint)' }}>{a.baseline_mean}h</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </GlassCard>

          <GlassCard title="Detector Config" icon={<Cpu size={14} />} iconColor="var(--text-dim)" status={null}>
            <div style={{ fontSize: 12 }}>
              {[
                ['PyTorch', ad.torch_available],
                ['NumPy', ad.numpy_available],
                ['SciPy', ad.scipy_available],
              ].map(([name, avail]) => (
                <div key={name} style={{ display: 'flex', justifyContent: 'space-between', padding: '10px 0', borderBottom: '1px solid var(--line)' }}>
                  <span style={{ fontWeight: 500 }}>{name}</span>
                  <StatusChip available={avail} label={avail ? 'installed' : 'missing'} />
                </div>
              ))}
              <div style={{ marginTop: 14, padding: '12px 14px', background: 'var(--glass)', borderRadius: 'var(--radius-sm)', fontSize: 11, color: 'var(--text-dim)', lineHeight: 1.6, border: '1px solid var(--glass-border)' }}>
                <strong style={{ color: 'var(--accent)' }}>LSTM Autoencoder</strong> trains per-asset on 48 synthetic ticks (~4h each), learns normal backup cadence, flags reconstruction errors {'>'}2&sigma; as anomalous.
              </div>
            </div>
          </GlassCard>
        </div>
      )}

      {/* ── ML FORECAST ── */}
      {activeSection === 'forecast' && (
        <div className="grid-2" style={{ gap: 14 }}>
          <GlassCard title="RPO Breach Forecast" icon={<TrendingUp size={14} />} iconColor="var(--warn)"
            status={<StatusChip available={ts.torch_available} label={`best: ${methodLabel(ts.best_method)}`} />}>
            {!mlPreds ? (
              <div className="loading"><div className="spinner" />running ML forecasting...</div>
            ) : mlPreds.ok === false ? (
              <div style={{ color: 'var(--text-dim)', fontSize: 12 }}>{mlPreds.error}</div>
            ) : (
              <>
                <div style={{ display: 'flex', gap: 24, marginBottom: 16 }}>
                  <BigNumber label="Breach predicted" value={mlPreds.breach_predicted_count} color={mlPreds.breach_predicted_count > 0 ? 'var(--p1)' : 'var(--ok)'} />
                  <BigNumber label="Horizon" value={`${mlPreds.horizon_steps} steps`} />
                </div>
                {mlPreds.method_distribution && (
                  <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 14 }}>
                    {Object.entries(mlPreds.method_distribution).map(([m, count]) => (
                      <span key={m} className="dataset-pill">{methodLabel(m)}: {count}</span>
                    ))}
                  </div>
                )}
                <div className="eyebrow" style={{ marginBottom: 8 }}>Assets at Risk</div>
                <div className="tbl-wrap" style={{ border: 'none' }}>
                  <table className="tbl" style={{ fontSize: 11 }}>
                    <thead><tr><th>Asset</th><th>T</th><th>Breach</th><th>Trend</th><th>Method</th><th>Conf</th></tr></thead>
                    <tbody>
                      {(mlPreds.at_risk || []).slice(0, 10).map(f => (
                        <tr key={f.asset_id}>
                          <td className="id">{f.asset_id}</td>
                          <td><span className="tag tier">T{f.tier}</span></td>
                          <td className="num" style={{ color: 'var(--p1)' }}>step {f.breach_at_step}</td>
                          <td><span style={{ fontSize: 10, color: f.trend === 'deteriorating' ? 'var(--p1)' : f.trend === 'improving' ? 'var(--ok)' : 'var(--text-dim)' }}>{f.trend}</span></td>
                          <td className="mono" style={{ fontSize: 10, color: 'var(--text-faint)' }}>{methodLabel(f.method)}</td>
                          <td className="num">{Math.round((f.confidence || 0) * 100)}%</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </>
            )}
          </GlassCard>

          <GlassCard title="Forecaster Stack" icon={<Layers size={14} />} iconColor="var(--violet)" status={null}>
            <div style={{ fontSize: 12 }}>
              {[
                ['PyTorch Transformer', 'Self-attention over backup windows', ts.torch_available && ts.numpy_available, 'var(--accent)'],
                ['Exponential Smoothing', 'Holt-Winters (statsmodels)', ts.statsmodels_available, 'var(--ok)'],
                ['Linear Regression', 'scipy.stats.linregress', ts.scipy_available, 'var(--warn)'],
                ['Python Extrapolation', 'Pure-Python slope, always available', true, 'var(--text-dim)'],
              ].map(([name, desc, avail, color]) => (
                <div key={name} style={{ display: 'flex', gap: 12, alignItems: 'flex-start', marginBottom: 14, paddingBottom: 14, borderBottom: '1px solid var(--line)' }}>
                  <div style={{
                    width: 28, height: 28, borderRadius: 8, display: 'grid', placeItems: 'center', flexShrink: 0,
                    background: `color-mix(in srgb, ${color} 10%, transparent)`,
                    border: `1px solid color-mix(in srgb, ${color} 15%, transparent)`,
                  }}>
                    {avail ? <CheckCircle size={12} style={{ color }} /> : <XCircle size={12} style={{ color: 'var(--text-faint)' }} />}
                  </div>
                  <div>
                    <div style={{ fontWeight: 600 }}>{name}</div>
                    <div style={{ fontSize: 11, color: 'var(--text-faint)' }}>{desc}</div>
                  </div>
                </div>
              ))}
            </div>
          </GlassCard>
        </div>
      )}

      {/* ── VISION ── */}
      {activeSection === 'vision' && (
        <div className="grid-2" style={{ gap: 14 }}>
          <GlassCard title="YOLO Visual Monitor" icon={<Eye size={14} />} iconColor="var(--violet)"
            status={<StatusChip available={ym.yolo_available} label={ym.yolo_available ? 'YOLOv8 active' : 'not installed'} />}>
            {!visual ? (
              <div className="loading"><div className="spinner" />generating visual analysis...</div>
            ) : visual.ok === false ? (
              <div style={{ fontSize: 12, color: 'var(--text-dim)' }}>{visual.error || 'Visual analysis unavailable'}</div>
            ) : (
              <>
                <div style={{ display: 'flex', gap: 24, marginBottom: 16 }}>
                  <BigNumber label="Visual anomaly" value={`${Math.round((visual.anomaly_score || 0) * 100)}%`} color={riskColor((visual.anomaly_score || 0) * 800)} />
                  <BigNumber label="Objects detected" value={visual.total_objects ?? '—'} />
                  <BigNumber label="Method" value={methodLabel(visual.method)} color="var(--text-dim)" />
                </div>
                {visual.warning_signals?.length > 0 && (
                  <div>
                    <div className="eyebrow" style={{ marginBottom: 8 }}>Warning Signals</div>
                    {visual.warning_signals.map((w, i) => (
                      <div key={i} className="mono" style={{ fontSize: 11, color: 'var(--warn)', marginBottom: 4 }}>
                        {w.class} — conf {Math.round(w.confidence * 100)}%
                      </div>
                    ))}
                  </div>
                )}
              </>
            )}
          </GlassCard>

          <GlassCard title="Vision Capabilities" icon={<Cpu size={14} />} iconColor="var(--text-dim)" status={null}>
            <div style={{ fontSize: 12 }}>
              {[
                ['YOLOv8 Detection', ym.yolo_available, 'Real screenshots & generated frames', 'var(--violet)'],
                ['Frame Generation', ym.pil_available && ym.numpy_available, 'Synthetic PNG from fleet state', 'var(--accent)'],
                ['Pixel Fallback', ym.pil_available, 'Red-channel analysis', 'var(--warn)'],
                ['OpenCV Processing', ym.cv2_available, 'Advanced image preprocessing', 'var(--ok)'],
              ].map(([name, avail, desc, color]) => (
                <div key={name} style={{ marginBottom: 12 }}>
                  <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                    <span style={{ fontWeight: 600, display: 'flex', alignItems: 'center', gap: 6 }}>
                      <span style={{ width: 6, height: 6, borderRadius: '50%', background: avail ? color : 'var(--text-faint)' }} />
                      {name}
                    </span>
                    <StatusChip available={avail} label={avail ? 'ready' : 'missing'} />
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--text-faint)', marginTop: 3, paddingLeft: 14 }}>{desc}</div>
                </div>
              ))}
            </div>
          </GlassCard>
        </div>
      )}

      {/* ── DATASETS ── */}
      {activeSection === 'datasets' && (
        <div className="grid-2" style={{ gap: 14 }}>
          <GlassCard title="HuggingFace Hub" icon={<Database size={14} />} iconColor="var(--accent)"
            status={<StatusChip available={hfDatasets?.hf_datasets_package} label={hfDatasets?.hf_datasets_package ? 'connected' : 'not installed'} />}>
            <div style={{ fontSize: 12 }}>
              {!hfDatasets ? (
                <div className="loading"><div className="spinner" /></div>
              ) : (
                <>
                  <div style={{ marginBottom: 12, color: 'var(--text-dim)' }}>
                    {hfDatasets.hf_datasets_package
                      ? `${hfDatasets.hf_dataset_keys?.length || 0} HF datasets — download on first request.`
                      : 'pip install datasets'}
                  </div>
                  {(hfDatasets.hf_dataset_keys || []).map(k => (
                    <div key={k} style={{ padding: '8px 12px', background: 'var(--glass)', borderRadius: 'var(--radius-xs)', marginBottom: 6, border: '1px solid var(--glass-border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                      <span className="mono" style={{ fontSize: 11, fontWeight: 500 }}>{k}</span>
                      <span style={{ fontSize: 9, color: 'var(--text-faint)' }}>GET /api/hf-datasets/{k}</span>
                    </div>
                  ))}
                </>
              )}
            </div>
          </GlassCard>

          <GlassCard title="Bundled LogHub" icon={<Layers size={14} />} iconColor="var(--ok)"
            status={<StatusChip available={true} label="always available" />}>
            <div style={{ fontSize: 12 }}>
              <div style={{ marginBottom: 12, color: 'var(--text-dim)' }}>16 real system log datasets — parsed at startup, no network needed.</div>
              {(hfDatasets?.local_loghub_keys || ['hdfs', 'apache', 'windows', 'linux', 'openssh', 'spark', 'hadoop']).map(k => (
                <div key={k} style={{ padding: '8px 12px', background: 'var(--glass)', borderRadius: 'var(--radius-xs)', marginBottom: 6, border: '1px solid var(--glass-border)', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                  <span className="mono" style={{ fontSize: 11, fontWeight: 500 }}>{k}</span>
                  <a href={`http://localhost:8000/api/hf-datasets/${k}?source=local`} target="_blank" rel="noreferrer" style={{ fontSize: 10, color: 'var(--accent)', textDecoration: 'none' }}>view &rarr;</a>
                </div>
              ))}
            </div>
          </GlassCard>
        </div>
      )}
    </div>
  );
}
