import React, { useEffect, useState } from 'react';
import { api } from './lib';
import SparklesBackground from './components/SparklesBackground';
import { Shield, Activity, Database, Layers, Cpu, Zap, Eye, Brain } from 'lucide-react';

export default function LandingPage({ onLaunch }) {
  const [health, setHealth] = useState(null);

  useEffect(() => {
    api('/api/health').then(setHealth).catch(() => {});
  }, []);

  return (
    <div className="landing">
      {/* Sparkle particle background */}
      <div style={{ position: 'fixed', inset: 0, zIndex: 0, pointerEvents: 'none' }}>
        <SparklesBackground
          particleColor="#38bdf8"
          particleDensity={50}
          minSize={0.4}
          maxSize={1.2}
          speed={0.6}
        />
      </div>

      <nav className="landing-nav" style={{ position: 'relative', zIndex: 2 }}>
        <div className="brand-lockup">
          <div className="brand-mark">S</div>
          <span className="brand-name">SENTRIX</span>
        </div>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span className="pill live" style={{ fontSize: 10 }}>
            <span className="dot" />v3.1
          </span>
          <button className="btn" onClick={onLaunch}>Open Console</button>
        </div>
      </nav>

      <div className="landing-hero" style={{ position: 'relative', zIndex: 2 }}>
        <span className="hero-status">
          <span style={{ width: 7, height: 7, borderRadius: '50%', background: 'var(--ok)', boxShadow: '0 0 10px var(--ok-glow)' }} />
          Autonomous recovery agent &middot; operational
        </span>

        <h1 className="hero-title">
          See it break.<br /><span className="accent">Recover first.</span>
        </h1>

        <p className="hero-sub">
          SENTRIX is an autonomous AI agent that perceives your fleet through deep learning,
          reasons with LLMs, detects anomalies with LSTM autoencoders, forecasts RPO breaches,
          and monitors infrastructure visually through YOLO &mdash; all in real-time.
        </p>

        <div className="hero-actions">
          <button className="btn btn-primary" onClick={onLaunch}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <Zap size={16} /> Launch Operations Console
            </span>
          </button>
          <a className="btn" href="https://github.com/logpai/loghub" target="_blank" rel="noreferrer">
            About the data
          </a>
        </div>

        {/* Feature bento grid */}
        <div style={{
          marginTop: 56, display: 'grid',
          gridTemplateColumns: 'repeat(4, 1fr)', gap: 12,
          animation: 'fadeInUp 1.3s ease',
        }}>
          {[
            { icon: <Brain size={18} />, title: 'Transformer NLP', desc: 'DistilBERT + BART zero-shot log classification', color: 'var(--accent)' },
            { icon: <Activity size={18} />, title: 'LSTM Anomaly', desc: 'Autoencoder time-series anomaly detection', color: 'var(--p1)' },
            { icon: <Eye size={18} />, title: 'YOLO Vision', desc: 'YOLOv8 visual infrastructure monitoring', color: 'var(--violet)' },
            { icon: <Cpu size={18} />, title: 'ML Forecasting', desc: 'Transformer + Holt-Winters RPO prediction', color: 'var(--ok)' },
          ].map((f, i) => (
            <div key={i} style={{
              background: 'var(--glass-card)',
              border: '1px solid var(--glass-border)',
              borderRadius: 'var(--radius)',
              padding: '20px 18px',
              backdropFilter: 'blur(16px)',
              transition: 'all 0.3s ease',
              cursor: 'default',
            }}
            onMouseEnter={e => { e.currentTarget.style.borderColor = 'var(--glass-border-hover)'; e.currentTarget.style.transform = 'translateY(-3px)'; }}
            onMouseLeave={e => { e.currentTarget.style.borderColor = 'var(--glass-border)'; e.currentTarget.style.transform = 'none'; }}
            >
              <div style={{
                width: 36, height: 36, borderRadius: 10,
                display: 'grid', placeItems: 'center',
                background: `color-mix(in srgb, ${f.color} 12%, transparent)`,
                color: f.color, marginBottom: 12,
                border: `1px solid color-mix(in srgb, ${f.color} 20%, transparent)`,
              }}>
                {f.icon}
              </div>
              <div style={{ fontWeight: 600, fontSize: 14, marginBottom: 4 }}>{f.title}</div>
              <div style={{ fontSize: 12, color: 'var(--text-dim)', lineHeight: 1.5 }}>{f.desc}</div>
            </div>
          ))}
        </div>

        {/* Stats strip */}
        <div className="hero-stats" style={{ marginTop: 20 }}>
          <div className="hero-stat">
            <div className="num">{health ? health.total_assets : '—'}</div>
            <div className="lbl">Monitored assets</div>
          </div>
          <div className="hero-stat">
            <div className="num">16</div>
            <div className="lbl">LogHub datasets</div>
          </div>
          <div className="hero-stat">
            <div className="num">{health ? `${health.backup_success_rate}%` : '—'}</div>
            <div className="lbl">Backup success</div>
          </div>
          <div className="hero-stat">
            <div className="num">5</div>
            <div className="lbl">ML modules</div>
          </div>
        </div>

        {/* Tech stack badges */}
        <div style={{
          marginTop: 24, display: 'flex', gap: 8, flexWrap: 'wrap',
          animation: 'fadeInUp 1.5s ease',
        }}>
          {['PyTorch', 'Transformers', 'YOLOv8', 'LSTM', 'FastAPI', 'React', 'WebSocket', 'HuggingFace'].map(t => (
            <span key={t} style={{
              fontFamily: 'var(--mono)', fontSize: 10, padding: '4px 10px',
              borderRadius: 100, background: 'var(--glass)',
              border: '1px solid var(--glass-border)',
              color: 'var(--text-faint)', letterSpacing: '0.04em',
            }}>{t}</span>
          ))}
        </div>
      </div>

      <footer className="landing-foot">
        Calibrated on real LogHub production logs &middot; multi-provider LLM reasoning &middot; deterministic fallback &middot; zero-cost stack
      </footer>
    </div>
  );
}
