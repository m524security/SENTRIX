import React, { useState, useEffect, useCallback, useRef } from 'react';
import Dashboard from './components/Dashboard';
import Assets from './components/Assets';
import AuditLog from './components/AuditLog';
import RestoreTests from './components/RestoreTests';
import RecoveryReadiness from './components/RecoveryReadiness';
import HeatMap from './components/HeatMap';
import LiveFeed from './components/LiveFeed';
import Simulation from './components/Simulation';
import ReasoningExplorer from './components/ReasoningExplorer';
import Predictions from './components/Predictions';
import AIInsights from './components/AIInsights';
import ChatBox from './components/ChatBox';
import Divergence from './components/Divergence';
import LandingPage from './LandingPage';
import { API_BASE, api, wsUrl } from './lib';
import {
  LayoutDashboard, Radio, TrendingUp, Brain, Lightbulb,
  FlaskConical, Server, Map, RotateCcw, ScrollText, GitBranch, ShieldCheck,
} from 'lucide-react';
import './App.css';

const TABS = [
  // PS284 deliverable — the headline view: proven recoverability, not backup status.
  { id: 'readiness', label: 'Readiness', icon: <ShieldCheck size={14} /> },
  { id: 'dashboard', label: 'Overview', icon: <LayoutDashboard size={14} /> },
  { id: 'livefeed', label: 'Live Feed', icon: <Radio size={14} /> },
  { id: 'predictions', label: 'Forecast', icon: <TrendingUp size={14} /> },
  { id: 'ai', label: 'AI Engine', icon: <Brain size={14} /> },
  { id: 'reasoning', label: 'Reasoning', icon: <Lightbulb size={14} /> },
  { id: 'divergence', label: 'Divergence', icon: <GitBranch size={14} /> },
  { id: 'simulation', label: 'Simulation', icon: <FlaskConical size={14} /> },
  { id: 'assets', label: 'Assets', icon: <Server size={14} /> },
  { id: 'heatmap', label: 'Risk Map', icon: <Map size={14} /> },
  { id: 'restore', label: 'Restore', icon: <RotateCcw size={14} /> },
  { id: 'audit', label: 'Audit', icon: <ScrollText size={14} /> },
];

export default function App() {
  const [view, setView] = useState('landing');
  const [tab, setTab] = useState('readiness');
  const [health, setHealth] = useState(null);
  const [provider, setProvider] = useState(null);
  const [ws, setWs] = useState(false);
  const [wsAuthError, setWsAuthError] = useState(false);
  const [cycles, setCycles] = useState([]);
  const sockRef = useRef(null);

  const refreshHealth = useCallback(() => {
    api('/api/health').then((h) => { setHealth(h); if (h.provider) setProvider(h.provider); }).catch(() => {});
  }, []);
  const refreshCycles = useCallback(() => {
    api('/api/cycles?limit=12').then((d) => setCycles(d.cycles || [])).catch(() => {});
  }, []);

  useEffect(() => {
    if (view !== 'dashboard') { setWs(false); return; }
    refreshHealth(); refreshCycles();
    const hv = setInterval(refreshHealth, 15000);
    return () => clearInterval(hv);
  }, [view, refreshHealth, refreshCycles]);

  useEffect(() => {
    if (view !== 'dashboard') return;
    let stopped = false;
    const connect = () => {
      if (stopped) return;
      let sock;
      try {
        sock = new WebSocket(wsUrl('/ws'));
      } catch { setTimeout(connect, 3000); return; }
      sockRef.current = sock;
      sock.onopen = () => setWs(true);
      sock.onmessage = (e) => {
        try {
          const msg = JSON.parse(e.data);
          if (msg.type === 'cycle_history') setCycles(msg.cycles || []);
          if (msg.type === 'cycle_update' && msg.cycle) {
            setCycles((prev) => [msg.cycle, ...prev.filter((c) => c.cycle_id !== msg.cycle.cycle_id)].slice(0, 24));
            refreshHealth();
          }
        } catch { /* ignore */ }
      };
      sock.onclose = (e) => {
        setWs(false);
        if (e.code === 4401) { setWsAuthError(true); return; }  // bad/missing token — don't loop
        if (!stopped) setTimeout(connect, 3000);
      };
      sock.onerror = () => { try { sock.close(); } catch { /* */ } };
    };
    connect();
    return () => { stopped = true; if (sockRef.current) sockRef.current.close(); };
  }, [view, refreshHealth]);

  if (view === 'landing') {
    return (
      <div className="app">
        <div className="console-grid" />
        <LandingPage onLaunch={() => setView('dashboard')} />
      </div>
    );
  }

  const providerActive = provider?.last_successful?.provider;
  const providerConfigured = provider?.configured_chain?.[0];
  const providerName = providerActive || providerConfigured || 'rule-engine';
  const isRule = providerName === 'rule_engine' || providerName === 'rule-engine' || (!providerActive && !providerConfigured);

  return (
    <div className="app">
      <div className="console-grid" />
      <div className="shell">
        <header className="topbar">
          <div className="topbar-left">
            <button className="icon-btn" onClick={() => setView('landing')} aria-label="Back to landing">&#8249;</button>
            <h1>SENTRIX</h1>
            <span className="crumb">/ {TABS.find((t) => t.id === tab)?.label}</span>
          </div>
          <div className="topbar-right">
            <span className={`pill provider ${isRule ? 'rule' : ''}`} title="Reasoning backend">
              {isRule ? 'RULE ENGINE' : `LLM · ${providerName}`}
            </span>
            {health && (
              <div className="health-strip">
                <span><b>{health.total_assets}</b> assets</span>
                <span className="sep">·</span>
                <span style={{ color: health.backup_success_rate > 80 ? 'var(--ok)' : 'var(--warn)' }}>
                  <b>{health.backup_success_rate}%</b> ok
                </span>
                <span className="sep">·</span>
                <span style={{ color: health.active_alerts > 0 ? 'var(--p1)' : 'var(--text-dim)' }}>
                  <b>{health.active_alerts}</b> alerts
                </span>
              </div>
            )}
            <span className={`pill ${ws ? 'live' : 'offline'}`}>
              <span className="dot" />{ws ? 'LIVE' : 'OFFLINE'}
            </span>
          </div>
        </header>

        <nav className="tabs">
          {TABS.map((t) => (
            <button key={t.id} className={`tab ${tab === t.id ? 'active' : ''}`} onClick={() => setTab(t.id)}>
              {t.icon}
              {t.label}
            </button>
          ))}
        </nav>

        <div className="workspace">
          <main className="content">
            {tab === 'dashboard' && <Dashboard cycles={cycles} health={health} />}
            {tab === 'livefeed' && <LiveFeed cycles={cycles} ws={ws} />}
            {tab === 'predictions' && <Predictions />}
            {tab === 'ai' && <AIInsights />}
            {tab === 'reasoning' && <ReasoningExplorer cycles={cycles} />}
            {tab === 'divergence' && <Divergence />}
            {tab === 'simulation' && <Simulation onResult={refreshCycles} />}
            {tab === 'assets' && <Assets />}
            {tab === 'heatmap' && <HeatMap />}
            {tab === 'readiness' && <RecoveryReadiness />}
            {tab === 'restore' && <RestoreTests />}
            {tab === 'audit' && <AuditLog />}
          </main>
          <ChatBox provider={provider} />
        </div>
      </div>
    </div>
  );
}
