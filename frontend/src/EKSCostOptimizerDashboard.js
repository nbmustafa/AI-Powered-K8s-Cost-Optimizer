import React, { useState, useEffect, useRef, useCallback } from 'react';
import ReactMarkdown from 'react-markdown';

// ── API client ────────────────────────────────────────────────────────────────
function useApi(apiUrl) {
  const get = useCallback(async (path, params = {}) => {
    const url = new URL(`${apiUrl}/api/v1${path}`, window.location.origin);
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
    const res = await fetch(url);
    if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
    return res.json();
  }, [apiUrl]);

  const post = useCallback(async (path, body) => {
    const url = `${apiUrl}/api/v1${path}`;
    const res = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(`API ${res.status}: ${res.statusText}`);
    return res.json();
  }, [apiUrl]);

  return { get, post };
}

// ── Formatting helpers ────────────────────────────────────────────────────────
const fmt = {
  currency: (n) =>
    new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 }).format(n),
  pct: (n) => `${n.toFixed(1)}%`,
};

const ACTION_CFG = {
  terminate: { label: 'Terminate', color: '#ff4444', bg: 'rgba(255,68,68,0.12)' },
  downsize:  { label: 'Downsize',  color: '#f59e0b', bg: 'rgba(245,158,11,0.12)' },
  upsize:    { label: 'Upsize',    color: '#60a5fa', bg: 'rgba(96,165,250,0.12)' },
  keep:      { label: 'Keep',      color: '#34d399', bg: 'rgba(52,211,153,0.12)' },
  rightsize: { label: 'Rightsize', color: '#a78bfa', bg: 'rgba(167,139,250,0.12)' },
};

// ── Sub-components ────────────────────────────────────────────────────────────

function Badge({ action }) {
  const cfg = ACTION_CFG[action] ?? ACTION_CFG.keep;
  return (
    <span style={{
      fontSize: 10, fontFamily: 'Space Mono, monospace', padding: '2px 8px',
      borderRadius: 4, background: cfg.bg, color: cfg.color,
      border: `1px solid ${cfg.color}30`,
    }}>
      {cfg.label}
    </span>
  );
}

function UtilBar({ pct, action }) {
  const cfg = ACTION_CFG[action] ?? ACTION_CFG.keep;
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
      <div style={{ flex: 1, height: 5, background: 'rgba(255,255,255,0.06)', borderRadius: 3, overflow: 'hidden' }}>
        <div style={{ width: `${Math.min(pct, 100)}%`, height: '100%', background: cfg.color, borderRadius: 3 }} />
      </div>
      <span style={{ fontSize: 11, color: '#555', fontFamily: 'Space Mono, monospace', minWidth: 34, textAlign: 'right' }}>
        {pct.toFixed(0)}%
      </span>
    </div>
  );
}

function MetricCard({ label, value, sub, accent }) {
  return (
    <div style={{
      background: 'rgba(255,255,255,0.03)',
      border: '1px solid rgba(255,255,255,0.07)',
      borderRadius: 12, padding: '18px 20px', position: 'relative', overflow: 'hidden',
    }}>
      <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 2, background: `linear-gradient(90deg, transparent, ${accent}, transparent)` }} />
      <div style={{ fontSize: 10, fontFamily: 'Space Mono, monospace', color: '#555', letterSpacing: '0.12em', textTransform: 'uppercase', marginBottom: 8 }}>{label}</div>
      <div style={{ fontSize: 26, fontWeight: 700, color: accent, fontFamily: 'Space Mono, monospace' }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: '#444', marginTop: 4, fontFamily: 'Space Mono, monospace' }}>{sub}</div>}
    </div>
  );
}

function SavingsRing({ pct }) {
  const r = 44, circ = 2 * Math.PI * r;
  return (
    <svg width={104} height={104} viewBox="0 0 104 104" style={{ flexShrink: 0 }}>
      <circle cx={52} cy={52} r={r} fill="none" stroke="rgba(255,255,255,0.05)" strokeWidth={9} />
      <circle cx={52} cy={52} r={r} fill="none" stroke="#34d399" strokeWidth={9}
        strokeDasharray={`${(pct / 100) * circ} ${circ}`}
        strokeDashoffset={circ / 4} strokeLinecap="round" />
      <text x={52} y={48} textAnchor="middle" fill="#34d399" fontSize={16} fontFamily="Space Mono, monospace" fontWeight={700}>{pct}%</text>
      <text x={52} y={62} textAnchor="middle" fill="#555" fontSize={8} fontFamily="Space Mono, monospace">SAVINGS</text>
    </svg>
  );
}

// ── Nodes table ───────────────────────────────────────────────────────────────
function NodeTable({ nodes }) {
  const [sortCol, setSortCol] = useState('savings_monthly');
  const [sortDir, setSortDir] = useState(-1);

  const sorted = [...nodes].sort((a, b) => {
    const av = a[sortCol] ?? 0, bv = b[sortCol] ?? 0;
    return typeof av === 'string' ? av.localeCompare(bv) * sortDir : (av - bv) * sortDir;
  });

  const Th = ({ col, label }) => (
    <th
      onClick={() => { setSortDir(sortCol === col ? -sortDir : -1); setSortCol(col); }}
      style={{ padding: '10px 12px', fontSize: 10, fontFamily: 'Space Mono, monospace', color: sortCol === col ? '#a78bfa' : '#444', letterSpacing: '0.1em', textTransform: 'uppercase', cursor: 'pointer', borderBottom: '1px solid rgba(255,255,255,0.06)', whiteSpace: 'nowrap' }}
    >
      {label}{sortCol === col ? (sortDir > 0 ? ' ↑' : ' ↓') : ''}
    </th>
  );

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            <Th col="node_name" label="Node" />
            <Th col="current_instance" label="Instance" />
            <Th col="current_cpu_util_pct" label="CPU p95" />
            <Th col="current_mem_util_pct" label="MEM p95" />
            <Th col="pod_count" label="Pods" />
            <Th col="current_cost_monthly" label="Monthly $" />
            <Th col="savings_monthly" label="Savings" />
            <th style={{ padding: '10px 12px', fontSize: 10, fontFamily: 'Space Mono, monospace', color: '#444', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>Action</th>
          </tr>
        </thead>
        <tbody>
          {sorted.map((n, i) => (
            <tr key={n.node_name} style={{ background: i % 2 ? 'rgba(255,255,255,0.015)' : 'transparent', borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
              <td style={{ padding: '11px 12px' }}>
                <div style={{ fontFamily: 'Space Mono, monospace', fontSize: 11, color: '#ccc', maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {n.node_name.split('.')[0]}
                </div>
                <div style={{ fontSize: 10, color: '#444', fontFamily: 'Space Mono, monospace', marginTop: 2 }}>
                  {n.labels?.['topology.kubernetes.io/zone'] ?? ''}
                </div>
              </td>
              <td style={{ padding: '11px 12px' }}>
                <span style={{ fontSize: 11, fontFamily: 'Space Mono, monospace', color: '#888', background: 'rgba(255,255,255,0.05)', padding: '2px 7px', borderRadius: 4 }}>{n.current_instance}</span>
              </td>
              <td style={{ padding: '11px 12px', minWidth: 120 }}><UtilBar pct={n.current_cpu_util_pct} action={n.action} /></td>
              <td style={{ padding: '11px 12px', minWidth: 120 }}><UtilBar pct={n.current_mem_util_pct} action={n.action} /></td>
              <td style={{ padding: '11px 12px', fontFamily: 'Space Mono, monospace', fontSize: 12, color: '#666' }}>{n.pod_count}</td>
              <td style={{ padding: '11px 12px', fontFamily: 'Space Mono, monospace', fontSize: 12, color: '#aaa' }}>{fmt.currency(n.current_cost_monthly)}</td>
              <td style={{ padding: '11px 12px', fontFamily: 'Space Mono, monospace', fontSize: 12, color: n.savings_monthly > 0 ? '#34d399' : n.savings_monthly < 0 ? '#f87171' : '#555' }}>
                {n.savings_monthly > 0 ? '+' : ''}{fmt.currency(n.savings_monthly)}/mo
              </td>
              <td style={{ padding: '11px 12px' }}><Badge action={n.action} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ── Pod table ─────────────────────────────────────────────────────────────────
function PodTable({ pods }) {
  const sorted = [...pods].sort((a, b) => b.annual_savings_usd - a.annual_savings_usd);
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse' }}>
        <thead>
          <tr>
            {['Workload', 'Namespace', 'CPU: req → actual', 'MEM: req → actual', 'Annual savings', 'Action'].map(h => (
              <th key={h} style={{ padding: '10px 12px', fontSize: 10, fontFamily: 'Space Mono, monospace', color: '#444', letterSpacing: '0.1em', textTransform: 'uppercase', borderBottom: '1px solid rgba(255,255,255,0.06)', whiteSpace: 'nowrap' }}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((p, i) => {
            const cpuWaste = p.current_request_cpu > 0
              ? ((p.current_request_cpu - p.actual_cpu_p95) / p.current_request_cpu * 100).toFixed(0)
              : 0;
            const memWaste = p.current_request_mem_gi > 0
              ? ((p.current_request_mem_gi - p.actual_mem_p95_gi) / p.current_request_mem_gi * 100).toFixed(0)
              : 0;
            return (
              <tr key={`${p.namespace}/${p.pod_name}`} style={{ background: i % 2 ? 'rgba(255,255,255,0.015)' : 'transparent', borderBottom: '1px solid rgba(255,255,255,0.04)' }}>
                <td style={{ padding: '11px 12px' }}>
                  <div style={{ fontFamily: 'Space Mono, monospace', fontSize: 12, color: '#ccc' }}>{p.owner_name}</div>
                  <div style={{ fontSize: 10, color: '#444', fontFamily: 'Space Mono, monospace', marginTop: 2 }}>{p.owner_kind}</div>
                </td>
                <td style={{ padding: '11px 12px' }}>
                  <span style={{ fontSize: 11, fontFamily: 'Space Mono, monospace', color: '#888', background: 'rgba(255,255,255,0.05)', padding: '2px 7px', borderRadius: 4 }}>{p.namespace}</span>
                </td>
                <td style={{ padding: '11px 12px', fontSize: 12 }}>
                  <span style={{ fontFamily: 'Space Mono, monospace', color: '#f87171' }}>{p.current_request_cpu}</span>
                  <span style={{ color: '#333', margin: '0 4px' }}>→</span>
                  <span style={{ fontFamily: 'Space Mono, monospace', color: '#34d399' }}>{p.actual_cpu_p95.toFixed(3)}</span>
                  <span style={{ fontSize: 10, color: '#555', marginLeft: 4, fontFamily: 'Space Mono, monospace' }}>({cpuWaste}% waste)</span>
                </td>
                <td style={{ padding: '11px 12px', fontSize: 12 }}>
                  <span style={{ fontFamily: 'Space Mono, monospace', color: '#f87171' }}>{p.current_request_mem_gi.toFixed(1)} GiB</span>
                  <span style={{ color: '#333', margin: '0 4px' }}>→</span>
                  <span style={{ fontFamily: 'Space Mono, monospace', color: '#34d399' }}>{p.actual_mem_p95_gi.toFixed(2)} GiB</span>
                  <span style={{ fontSize: 10, color: '#555', marginLeft: 4, fontFamily: 'Space Mono, monospace' }}>({memWaste}%)</span>
                </td>
                <td style={{ padding: '11px 12px', fontFamily: 'Space Mono, monospace', fontSize: 12, color: '#34d399' }}>
                  {fmt.currency(p.annual_savings_usd)}/yr
                </td>
                <td style={{ padding: '11px 12px' }}><Badge action={p.action} /></td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ── AI Chat panel ─────────────────────────────────────────────────────────────
function AIChat({ apiUrl, report }) {
  const { post } = useApi(apiUrl);
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const bottomRef = useRef(null);

  const QUICK_PROMPTS = [
    'What are the top 3 quick wins?',
    'Generate kubectl patch commands for top pods',
    'Explain Karpenter consolidation setup',
    'Which changes have highest risk?',
  ];

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages, loading]);

  const send = useCallback(async (text) => {
    if (!text.trim() || loading) return;
    const question = text.trim();
    setInput('');
    setLoading(true);
    setMessages(prev => [...prev, { role: 'user', content: question }]);

    try {
      // Build history from existing messages for multi-turn context
      const history = messages.map(m => ({ role: m.role, content: m.content }));
      const data = await post('/ai/chat', { question, history });
      setMessages(prev => [...prev, { role: 'assistant', content: data.answer }]);
    } catch (e) {
      setMessages(prev => [...prev, { role: 'assistant', content: `⚠️ Error: ${e.message}` }]);
    } finally {
      setLoading(false);
    }
  }, [messages, loading, post]);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: 500 }}>
      {/* Message list */}
      <div style={{ flex: 1, overflowY: 'auto', padding: '16px 0', display: 'flex', flexDirection: 'column', gap: 12 }}>
        {messages.length === 0 && (
          <div style={{ textAlign: 'center', color: '#444', paddingTop: 40 }}>
            <div style={{ fontSize: 28, marginBottom: 10 }}>⚡</div>
            <div style={{ fontFamily: 'Space Mono, monospace', fontSize: 12, color: '#444' }}>
              Ask Claude anything about your cluster costs
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, justifyContent: 'center', marginTop: 18 }}>
              {QUICK_PROMPTS.map(q => (
                <button key={q} onClick={() => send(q)}
                  style={{ fontSize: 11, fontFamily: 'Space Mono, monospace', padding: '6px 12px', background: 'rgba(167,139,250,0.1)', border: '1px solid rgba(167,139,250,0.3)', borderRadius: 6, color: '#a78bfa', cursor: 'pointer' }}>
                  {q}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) => (
          <div key={i} style={{ display: 'flex', gap: 10, alignItems: 'flex-start', flexDirection: m.role === 'user' ? 'row-reverse' : 'row' }}>
            <div style={{ width: 26, height: 26, borderRadius: 6, background: m.role === 'user' ? 'rgba(167,139,250,0.3)' : 'rgba(52,211,153,0.2)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, flexShrink: 0, fontFamily: 'Space Mono, monospace' }}>
              {m.role === 'user' ? 'U' : 'AI'}
            </div>
            <div style={{
              maxWidth: '84%',
              background: m.role === 'user' ? 'rgba(167,139,250,0.1)' : 'rgba(255,255,255,0.03)',
              border: `1px solid ${m.role === 'user' ? 'rgba(167,139,250,0.2)' : 'rgba(255,255,255,0.06)'}`,
              borderRadius: 10, padding: '10px 14px', fontSize: 12, lineHeight: 1.7, color: '#c4c8d0',
            }}>
              {m.role === 'assistant'
                ? <ReactMarkdown>{m.content}</ReactMarkdown>
                : <span style={{ color: '#c4b5fd' }}>{m.content}</span>}
            </div>
          </div>
        ))}
        {loading && (
          <div style={{ display: 'flex', gap: 10, alignItems: 'flex-start' }}>
            <div style={{ width: 26, height: 26, borderRadius: 6, background: 'rgba(52,211,153,0.2)', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: 10, fontFamily: 'Space Mono, monospace' }}>AI</div>
            <div style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 10, padding: '12px 14px', display: 'flex', gap: 4, alignItems: 'center' }}>
              {[0, 1, 2].map(j => (
                <div key={j} style={{ width: 6, height: 6, borderRadius: '50%', background: '#34d399', animation: `pulse 1.2s ease-in-out ${j * 0.2}s infinite` }} />
              ))}
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {/* Input row */}
      <div style={{ display: 'flex', gap: 8, paddingTop: 12, borderTop: '1px solid rgba(255,255,255,0.06)' }}>
        <input
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(input); } }}
          placeholder="Ask about costs, optimizations, commands..."
          style={{ flex: 1, background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(255,255,255,0.1)', borderRadius: 8, padding: '10px 14px', color: '#e2e8f0', fontSize: 12, fontFamily: 'Space Mono, monospace', outline: 'none' }}
        />
        <button onClick={() => send(input)} disabled={loading || !input.trim()}
          style={{ padding: '10px 18px', background: loading ? 'rgba(167,139,250,0.2)' : 'rgba(167,139,250,0.8)', border: 'none', borderRadius: 8, color: '#fff', cursor: loading ? 'not-allowed' : 'pointer', fontFamily: 'Space Mono, monospace', fontSize: 12, fontWeight: 700 }}>
          {loading ? '...' : 'Send'}
        </button>
      </div>
    </div>
  );
}

// ── Loading / Error states ────────────────────────────────────────────────────
function LoadingScreen() {
  return (
    <div style={{ minHeight: '100vh', background: '#080c10', display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center' }}>
      <div style={{ fontSize: 12, color: '#333', letterSpacing: '0.2em', fontFamily: 'Space Mono, monospace', animation: 'pulse 1.5s ease-in-out infinite' }}>
        SCANNING CLUSTER...
      </div>
      <div style={{ width: 280, height: 2, background: 'rgba(255,255,255,0.05)', borderRadius: 1, marginTop: 24, overflow: 'hidden' }}>
        <div style={{ height: '100%', background: 'linear-gradient(90deg, #a78bfa, #34d399)', borderRadius: 1, animation: 'loadbar 1.2s ease-in-out infinite' }} />
      </div>
      <style>{`
        @keyframes loadbar { 0%{width:0;margin-left:0} 50%{width:100%;margin-left:0} 100%{width:0;margin-left:100%} }
        @keyframes pulse { 0%,100%{opacity:0.4} 50%{opacity:1} }
      `}</style>
    </div>
  );
}

function ErrorBanner({ message, onRetry }) {
  return (
    <div style={{ background: 'rgba(255,68,68,0.1)', border: '1px solid rgba(255,68,68,0.3)', borderRadius: 10, padding: '16px 20px', margin: '24px 32px', fontFamily: 'Space Mono, monospace', fontSize: 12, color: '#f87171', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
      <span>⚠ {message}</span>
      <button onClick={onRetry} style={{ background: 'rgba(255,68,68,0.2)', border: '1px solid rgba(255,68,68,0.4)', borderRadius: 6, color: '#f87171', cursor: 'pointer', padding: '4px 12px', fontFamily: 'Space Mono, monospace', fontSize: 11 }}>
        Retry
      </button>
    </div>
  );
}

// ── Main Dashboard ────────────────────────────────────────────────────────────
export default function EKSCostOptimizer({ apiUrl = '' }) {
  const { get } = useApi(apiUrl);
  const [tab, setTab] = useState('overview');
  const [report, setReport] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);

  const fetchReport = useCallback(async (forceRefresh = false) => {
    try {
      setError(null);
      const data = await get('/cost-report', forceRefresh ? { force_refresh: true } : {});
      setReport(data.data);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [get]);

  useEffect(() => { fetchReport(); }, [fetchReport]);

  const handleRefresh = () => { setRefreshing(true); fetchReport(true); };

  if (loading) return <LoadingScreen />;

  const TABS = [
    { id: 'overview', label: 'Overview' },
    { id: 'nodes',    label: 'Nodes' },
    { id: 'pods',     label: 'Workloads' },
    { id: 'ai',       label: 'AI Advisor' },
  ];

  const topPods = (report?.pod_recommendations ?? [])
    .filter(p => p.action === 'rightsize')
    .sort((a, b) => b.annual_savings_usd - a.annual_savings_usd)
    .slice(0, 25);

  return (
    <div style={{ minHeight: '100vh', background: '#080c10', color: '#e2e8f0', fontFamily: 'system-ui, sans-serif' }}>
      <style>{`
        @keyframes pulse { 0%,100%{opacity:0.5} 50%{opacity:1} }
        @keyframes fadeIn { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }
        .fade-in { animation: fadeIn 0.35s ease forwards; }
        * { box-sizing: border-box; }
        pre { background: rgba(255,255,255,0.05); border-radius: 6px; padding: 10px 14px; overflow-x: auto; font-size: 11px; }
        code { font-family: 'Space Mono', monospace; font-size: 11px; }
        ::-webkit-scrollbar { width: 5px; height: 5px; }
        ::-webkit-scrollbar-thumb { background: #1e2733; border-radius: 3px; }
      `}</style>

      {/* ── Header ── */}
      <header style={{ borderBottom: '1px solid rgba(255,255,255,0.06)', background: 'rgba(8,12,16,0.95)', position: 'sticky', top: 0, zIndex: 100, backdropFilter: 'blur(10px)' }}>
        <div style={{ maxWidth: 1400, margin: '0 auto', padding: '0 28px', display: 'flex', alignItems: 'center', justifyContent: 'space-between', height: 58 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
              <div style={{ width: 7, height: 7, borderRadius: '50%', background: '#34d399', boxShadow: '0 0 6px #34d399' }} />
              <span style={{ fontFamily: 'Space Mono, monospace', fontSize: 12, fontWeight: 700, letterSpacing: '0.06em' }}>EKS COST OPTIMIZER</span>
            </div>
            <div style={{ width: 1, height: 14, background: 'rgba(255,255,255,0.1)' }} />
            <span style={{ fontFamily: 'Space Mono, monospace', fontSize: 11, color: '#444' }}>
              {report?.cluster_name ?? '—'}
            </span>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
            <span style={{ fontFamily: 'Space Mono, monospace', fontSize: 10, color: '#333' }}>
              {report?.generated_at ? new Date(report.generated_at).toLocaleTimeString() : ''}
            </span>
            <button onClick={handleRefresh} disabled={refreshing}
              style={{ fontSize: 10, fontFamily: 'Space Mono, monospace', padding: '4px 10px', background: 'transparent', border: '1px solid rgba(255,255,255,0.12)', borderRadius: 6, color: '#666', cursor: refreshing ? 'not-allowed' : 'pointer' }}>
              {refreshing ? 'Refreshing...' : '↻ Refresh'}
            </button>
          </div>
        </div>
      </header>

      <div style={{ maxWidth: 1400, margin: '0 auto', padding: '28px 28px' }}>

        {error && <ErrorBanner message={error} onRetry={() => fetchReport(true)} />}

        {report && (
          <>
            {/* ── Savings Banner ── */}
            <div className="fade-in" style={{ background: 'linear-gradient(135deg, rgba(52,211,153,0.07), rgba(167,139,250,0.07))', border: '1px solid rgba(52,211,153,0.18)', borderRadius: 14, padding: '22px 28px', marginBottom: 28, display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 20 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 20 }}>
                <SavingsRing pct={Math.round(report.savings_percentage)} />
                <div>
                  <div style={{ fontFamily: 'Space Mono, monospace', fontSize: 10, color: '#555', letterSpacing: '0.14em', marginBottom: 6 }}>OPTIMIZATION OPPORTUNITY</div>
                  <div style={{ fontSize: 30, fontWeight: 700, color: '#34d399', fontFamily: 'Space Mono, monospace' }}>
                    {fmt.currency(report.potential_monthly_savings)}<span style={{ fontSize: 13, color: '#444' }}>/mo</span>
                  </div>
                  <div style={{ fontSize: 12, color: '#555', fontFamily: 'Space Mono, monospace', marginTop: 4 }}>
                    {fmt.currency(report.potential_annual_savings)} potential annual savings
                  </div>
                </div>
              </div>
              <div style={{ display: 'flex', gap: 28, flexWrap: 'wrap' }}>
                {[
                  { label: 'Idle Nodes',      value: report.summary?.nodes_to_terminate ?? 0, color: '#ff4444' },
                  { label: 'Oversized Nodes', value: report.summary?.nodes_to_downsize  ?? 0, color: '#f59e0b' },
                  { label: 'Pods to Rightsize', value: report.summary?.pods_to_rightsize ?? 0, color: '#a78bfa' },
                  { label: 'CPU Waste',       value: `${report.summary?.pod_cpu_waste_cores ?? 0} cores`, color: '#60a5fa' },
                ].map(s => (
                  <div key={s.label} style={{ textAlign: 'center' }}>
                    <div style={{ fontFamily: 'Space Mono, monospace', fontSize: 22, fontWeight: 700, color: s.color }}>{s.value}</div>
                    <div style={{ fontFamily: 'Space Mono, monospace', fontSize: 10, color: '#444', marginTop: 2 }}>{s.label}</div>
                  </div>
                ))}
              </div>
            </div>

            {/* ── Tabs ── */}
            <div style={{ display: 'flex', gap: 2, marginBottom: 24, background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 9, padding: 3, width: 'fit-content' }}>
              {TABS.map(t => (
                <button key={t.id} onClick={() => setTab(t.id)}
                  style={{ fontFamily: 'Space Mono, monospace', fontSize: 11, padding: '7px 18px', borderRadius: 6, border: 'none', cursor: 'pointer', letterSpacing: '0.07em', fontWeight: 700, transition: 'all 0.15s', background: tab === t.id ? 'rgba(167,139,250,0.18)' : 'transparent', color: tab === t.id ? '#a78bfa' : '#444' }}>
                  {t.label.toUpperCase()}
                </button>
              ))}
            </div>

            {/* ── Overview ── */}
            {tab === 'overview' && (
              <div className="fade-in">
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 14, marginBottom: 28 }}>
                  <MetricCard label="Current Monthly Cost" value={fmt.currency(report.current_monthly_cost)} sub="On-demand compute" accent="#f87171" />
                  <MetricCard label="Optimized Cost" value={fmt.currency(report.optimized_monthly_cost)} sub="After all changes" accent="#34d399" />
                  <MetricCard label="Annual Savings" value={fmt.currency(report.potential_annual_savings)} sub="Nodes + workloads" accent="#a78bfa" />
                  <MetricCard label="Total Nodes" value={`${report.total_nodes}`} sub={`${report.summary?.nodes_to_terminate ?? 0} idle · ${report.summary?.nodes_to_downsize ?? 0} oversized`} accent="#60a5fa" />
                  <MetricCard label="Total Pods" value={`${report.total_pods}`} sub={`${report.summary?.pods_to_rightsize ?? 0} need rightsizing`} accent="#f59e0b" />
                  <MetricCard label="Memory Waste" value={`${report.summary?.pod_mem_waste_gi ?? 0} GiB`} sub="Unused memory reserved" accent="#ec4899" />
                </div>
                {/* Metrics source badge */}
                {report.summary?.metrics_sources && (
                  <div style={{ display: 'flex', gap: 10, marginBottom: 20 }}>
                    {[
                      { key: 'prometheus', label: 'Prometheus', color: '#f59e0b' },
                      { key: 'cloudwatch', label: 'CloudWatch', color: '#60a5fa' },
                    ].map(s => (
                      <span key={s.key} style={{ fontSize: 10, fontFamily: 'Space Mono, monospace', padding: '3px 9px', borderRadius: 4, background: report.summary.metrics_sources[s.key] ? `rgba(${s.color === '#f59e0b' ? '245,158,11' : '96,165,250'},0.12)` : 'rgba(255,255,255,0.04)', color: report.summary.metrics_sources[s.key] ? s.color : '#333', border: `1px solid ${report.summary.metrics_sources[s.key] ? s.color + '30' : 'transparent'}` }}>
                        {report.summary.metrics_sources[s.key] ? '✓' : '○'} {s.label}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            )}

            {/* ── Nodes ── */}
            {tab === 'nodes' && (
              <div className="fade-in">
                <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 12, overflow: 'hidden' }}>
                  <div style={{ padding: '18px 20px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                    <span style={{ fontFamily: 'Space Mono, monospace', fontSize: 11, color: '#555' }}>
                      NODE ANALYSIS — {report.node_recommendations.length} nodes
                    </span>
                  </div>
                  <NodeTable nodes={report.node_recommendations} />
                </div>
              </div>
            )}

            {/* ── Pods ── */}
            {tab === 'pods' && (
              <div className="fade-in">
                <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(255,255,255,0.06)', borderRadius: 12, overflow: 'hidden' }}>
                  <div style={{ padding: '18px 20px', borderBottom: '1px solid rgba(255,255,255,0.06)' }}>
                    <div style={{ fontFamily: 'Space Mono, monospace', fontSize: 11, color: '#555' }}>
                      WORKLOAD RIGHT-SIZING — top {topPods.length} by annual savings
                    </div>
                    <div style={{ fontFamily: 'Space Mono, monospace', fontSize: 10, color: '#444', marginTop: 5 }}>
                      CPU waste: <span style={{ color: '#f59e0b' }}>{report.summary?.pod_cpu_waste_cores} cores</span>
                      &nbsp;·&nbsp;
                      Memory waste: <span style={{ color: '#f59e0b' }}>{report.summary?.pod_mem_waste_gi} GiB</span>
                    </div>
                  </div>
                  <PodTable pods={topPods} />
                </div>
              </div>
            )}

            {/* ── AI ── */}
            {tab === 'ai' && (
              <div className="fade-in">
                <div style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(167,139,250,0.18)', borderRadius: 12, overflow: 'hidden' }}>
                  <div style={{ padding: '18px 20px', borderBottom: '1px solid rgba(255,255,255,0.06)', display: 'flex', alignItems: 'center', gap: 10 }}>
                    <div style={{ width: 7, height: 7, borderRadius: '50%', background: '#a78bfa', boxShadow: '0 0 6px #a78bfa' }} />
                    <span style={{ fontFamily: 'Space Mono, monospace', fontSize: 11, color: '#a78bfa' }}>AI COST ADVISOR</span>
                    <span style={{ fontFamily: 'Space Mono, monospace', fontSize: 10, color: '#444' }}>Powered by Claude · calls /api/v1/ai/chat</span>
                  </div>
                  <div style={{ padding: '0 20px 20px' }}>
                    <AIChat apiUrl={apiUrl} report={report} />
                  </div>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
