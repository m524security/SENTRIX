import React, { useState, useRef, useEffect } from 'react';
import { api } from '../lib';

const SUGGESTIONS = [
  'What should I prioritize right now?',
  'Which assets are about to breach RPO?',
  'Summarize fleet health',
  'Why was the top asset escalated?',
];

export default function ChatBox({ provider }) {
  const [messages, setMessages] = useState([
    { role: 'bot', content: "I'm SENTRIX. Ask me about fleet health, active escalations, or what to prioritize — I answer from the live state of your assets." },
  ]);
  const [input, setInput] = useState('');
  const [busy, setBusy] = useState(false);
  const logRef = useRef(null);

  useEffect(() => { if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight; }, [messages, busy]);

  const send = async (text) => {
    const message = (text ?? input).trim();
    if (!message || busy) return;
    const history = messages.filter((m) => m.role !== 'system').map((m) => ({ role: m.role === 'bot' ? 'assistant' : 'user', content: m.content }));
    setMessages((m) => [...m, { role: 'user', content: message }]);
    setInput(''); setBusy(true);
    try {
      const d = await api('/api/chat', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message, history }),
      });
      setMessages((m) => [...m, { role: 'bot', content: d.reply || '(no response)', meta: d.provider }]);
    } catch (e) {
      setMessages((m) => [...m, { role: 'bot', content: `I couldn't reach the backend (${e}). Is the API running?` }]);
    }
    setBusy(false);
  };

  const providerName = provider?.last_successful?.provider || provider?.configured_chain?.[0] || 'rule-engine';

  return (
    <aside className="chat-rail">
      <div className="chat-head">
        <div className="title">
          <span style={{ width: 8, height: 8, borderRadius: '50%', background: 'var(--accent)', boxShadow: '0 0 8px var(--accent)' }} />
          SENTRIX COPILOT
        </div>
        <div className="sub">Grounded in live fleet state · {providerName}</div>
      </div>

      <div className="chat-log" ref={logRef}>
        {messages.map((m, i) => (
          <div key={i} className={`chat-msg ${m.role === 'user' ? 'user' : 'bot'}`}>
            {m.role === 'bot' && <div className="who">SENTRIX{m.meta ? ` · ${m.meta}` : ''}</div>}
            <div className="bubble">{m.content}</div>
          </div>
        ))}
        {busy && (
          <div className="chat-msg bot">
            <div className="who">SENTRIX</div>
            <div className="bubble"><span className="typing"><span /><span /><span /></span></div>
          </div>
        )}
      </div>

      {messages.length <= 1 && (
        <div className="chat-suggest">
          {SUGGESTIONS.map((s) => <button key={s} className="chat-chip" onClick={() => send(s)}>{s}</button>)}
        </div>
      )}

      <div className="chat-input">
        <input
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
          placeholder="Ask about the fleet…"
          disabled={busy}
        />
        <button className="chat-send" onClick={() => send()} disabled={busy || !input.trim()} aria-label="Send">→</button>
      </div>
    </aside>
  );
}
