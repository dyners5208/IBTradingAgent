import { useState, useCallback } from 'react'
import { PieChart, Pie, Cell, Tooltip, ResponsiveContainer } from 'recharts'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const ROOT_CAUSE_COLORS = {
  IV_REGIME_MISMATCH:  '#f59e0b',
  MOMENTUM_COLLAPSE:   '#ef4444',
  PREMATURE_STOP:      '#f97316',
  BAD_ENTRY_TIMING:    '#f97316',
  THETA_BLEED:         '#3b82f6',
  ASSIGNMENT:          '#8b5cf6',
  ROLL_DECLINED:       '#6b7280',
  UNCLASSIFIED:        '#374151',
}

const ROOT_CAUSE_LABELS = {
  IV_REGIME_MISMATCH:  'IV Regime Mismatch',
  MOMENTUM_COLLAPSE:   'Momentum Collapse',
  PREMATURE_STOP:      'Premature Stop',
  BAD_ENTRY_TIMING:    'Bad Entry Timing',
  THETA_BLEED:         'Theta Bleed',
  ASSIGNMENT:          'Assignment',
  ROLL_DECLINED:       'Roll Declined',
  UNCLASSIFIED:        'Unclassified',
}

const fmt = v =>
  `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function RootCauseBadge({ cause }) {
  const color = ROOT_CAUSE_COLORS[cause] || '#6b7280'
  const label = ROOT_CAUSE_LABELS[cause] || cause || '—'
  return (
    <span style={{
      background: `${color}22`,
      color,
      border: `1px solid ${color}55`,
      borderRadius: 12,
      padding: '2px 8px',
      fontSize: 10,
      fontWeight: 700,
      whiteSpace: 'nowrap',
    }}>
      {label}
    </span>
  )
}

function LossBreakdownChart({ data }) {
  if (!data?.length) return (
    <div className="empty" style={{ padding: '24px 0' }}>
      No analyzed losses yet — click Analyze on a losing trade below.
    </div>
  )
  const total = data.reduce((s, d) => s + d.count, 0)

  const renderLabel = ({ cx, cy, midAngle, innerRadius, outerRadius, percent }) => {
    if (percent < 0.08) return null
    const RADIAN = Math.PI / 180
    const r = innerRadius + (outerRadius - innerRadius) * 0.6
    const x = cx + r * Math.cos(-midAngle * RADIAN)
    const y = cy + r * Math.sin(-midAngle * RADIAN)
    return (
      <text x={x} y={y} fill="#fff" textAnchor="middle"
            dominantBaseline="central" fontSize={11} fontWeight={700}>
        {Math.round(percent * 100)}%
      </text>
    )
  }

  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 32, flexWrap: 'wrap' }}>
      <ResponsiveContainer width={200} height={200}>
        <PieChart>
          <Pie data={data} dataKey="count" nameKey="root_cause"
               cx="50%" cy="50%" innerRadius={50} outerRadius={90}
               paddingAngle={2} labelLine={false} label={renderLabel}>
            {data.map(d => (
              <Cell key={d.root_cause}
                    fill={ROOT_CAUSE_COLORS[d.root_cause] || '#6b7280'} />
            ))}
          </Pie>
          <Tooltip
            formatter={(v, name) => [v, ROOT_CAUSE_LABELS[name] || name]}
            contentStyle={{ background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 6, fontSize: 12 }}
          />
        </PieChart>
      </ResponsiveContainer>

      <div style={{ flex: 1, minWidth: 220 }}>
        {data.map(d => (
          <div key={d.root_cause} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 8 }}>
            <span style={{
              width: 10, height: 10, borderRadius: '50%', flexShrink: 0,
              background: ROOT_CAUSE_COLORS[d.root_cause] || '#6b7280',
            }} />
            <span style={{ color: 'var(--text2)', fontSize: 12, flex: 1 }}>
              {ROOT_CAUSE_LABELS[d.root_cause] || d.root_cause}
            </span>
            <span style={{ fontWeight: 700, fontSize: 12, minWidth: 24, textAlign: 'right', color: 'var(--text)' }}>
              {d.count}
            </span>
            <span className="pnl-neg" style={{ fontSize: 11, minWidth: 70, textAlign: 'right' }}>
              {fmt(d.total_pnl)}
            </span>
          </div>
        ))}
        <div style={{ borderTop: '1px solid var(--border)', paddingTop: 6, marginTop: 4, display: 'flex', justifyContent: 'space-between', color: 'var(--text3)', fontSize: 11 }}>
          <span>Total analyzed: {total}</span>
        </div>
      </div>
    </div>
  )
}

function WeeklyInsights() {
  const [loading, setLoading] = useState(false)
  const [data, setData]       = useState(null)
  const [error, setError]     = useState(null)

  const load = useCallback(async (force = false) => {
    setLoading(true)
    setError(null)
    try {
      const url = force ? '/api/postmortem/insights?force=true' : '/api/postmortem/insights'
      const res = await fetch(url)
      if (res.status === 503) {
        setError('Configure the ANTHROPIC_API_KEY environment variable to generate weekly insights.')
        return
      }
      if (!res.ok) throw new Error(await res.text())
      setData(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [])

  return (
    <div className="chart-card" style={{ marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12 }}>
        <h3 style={{ margin: 0 }}>Weekly Pattern Insights (last 30 days)</h3>
        <button
          onClick={() => load(true)}
          disabled={loading}
          style={{
            background: 'var(--accent)', color: '#fff', border: 'none',
            borderRadius: 6, padding: '5px 12px', fontSize: 11, cursor: 'pointer',
            opacity: loading ? 0.6 : 1,
          }}
        >
          {loading ? 'Generating…' : data ? 'Regenerate' : 'Generate Insights'}
        </button>
      </div>

      {!data && !error && !loading && (
        <div style={{ color: 'var(--text3)', fontSize: 12, padding: '12px 0' }}>
          Click "Generate Insights" to analyze the last 30 days of trades with Claude AI.
          Requires ANTHROPIC_API_KEY environment variable.
        </div>
      )}
      {loading && <div className="loading">Analyzing trades…</div>}
      {error && (
        <div style={{
          background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.25)',
          borderRadius: 6, padding: '10px 14px', color: 'var(--red)', fontSize: 12,
        }}>
          {error}
        </div>
      )}
      {data?.patterns && (
        <div>
          <pre style={{
            whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            fontFamily: 'var(--font-mono, monospace)', fontSize: 12,
            color: 'var(--text2)', lineHeight: 1.6,
            background: 'var(--surface)', borderRadius: 6,
            padding: '12px 14px', margin: 0,
          }}>
            {data.patterns}
          </pre>
          <div style={{ marginTop: 8, color: 'var(--text3)', fontSize: 10 }}>
            Generated {data.generated_at ? String(data.generated_at).slice(0, 19).replace('T', ' ') : ''} · {data.trades_analyzed} trades analyzed
          </div>
        </div>
      )}
      {data?.error && (
        <div style={{ color: 'var(--red)', fontSize: 12 }}>{data.error}</div>
      )}
    </div>
  )
}

function RecommendationsTable({ recs }) {
  if (!recs?.length) return null
  return (
    <div style={{ marginTop: 12 }}>
      <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--text3)', textTransform: 'uppercase', letterSpacing: '0.8px', marginBottom: 6 }}>
        Recommendations
      </div>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
        <thead>
          <tr>
            <th style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--text3)', fontWeight: 600 }}>Constant</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', color: 'var(--text3)', fontWeight: 600 }}>Current</th>
            <th style={{ textAlign: 'right', padding: '4px 8px', color: 'var(--text3)', fontWeight: 600 }}>Suggested</th>
            <th style={{ textAlign: 'left', padding: '4px 8px', color: 'var(--text3)', fontWeight: 600 }}>Rationale</th>
          </tr>
        </thead>
        <tbody>
          {recs.map((r, i) => (
            <tr key={i} style={{ borderTop: '1px solid var(--border)' }}>
              <td style={{ padding: '5px 8px', fontFamily: 'monospace', color: 'var(--accent)' }}>{r.constant}</td>
              <td style={{ padding: '5px 8px', textAlign: 'right', color: 'var(--text3)' }}>{r.current}</td>
              <td style={{ padding: '5px 8px', textAlign: 'right', color: 'var(--green)', fontWeight: 700 }}>{r.suggested}</td>
              <td style={{ padding: '5px 8px', color: 'var(--text2)' }}>{r.rationale}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function AnalysisPanel({ trade, onClose }) {
  const [loading, setLoading] = useState(false)
  const [result, setResult]   = useState(
    trade.root_cause ? {
      root_cause:           trade.root_cause,
      root_cause_detail:    trade.root_cause_detail,
      claude_analysis:      trade.claude_analysis,
      claude_recommendations: Array.isArray(trade.claude_recommendations)
        ? trade.claude_recommendations : [],
    } : null
  )
  const [error, setError] = useState(null)

  const analyze = useCallback(async (force = false) => {
    setLoading(true)
    setError(null)
    try {
      const url = `/api/postmortem/trade/${trade.id}${force ? '?force=true' : ''}`
      const res = await fetch(url)
      if (!res.ok) throw new Error(await res.text())
      setResult(await res.json())
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }, [trade.id])

  // Auto-fetch if no result yet
  useState(() => { if (!result) analyze() }, [])

  return (
    <tr>
      <td colSpan={8} style={{ padding: 0 }}>
        <div style={{
          background: 'var(--surface2)', borderLeft: '3px solid var(--accent)',
          margin: '0 0 2px', padding: '14px 16px',
        }}>
          {loading && <div className="loading" style={{ fontSize: 12 }}>Analyzing with Claude…</div>}
          {error && <div style={{ color: 'var(--red)', fontSize: 12 }}>{error}</div>}

          {result && (
            <>
              <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 10 }}>
                <RootCauseBadge cause={result.root_cause} />
                <span style={{ color: 'var(--text2)', fontSize: 12 }}>{result.root_cause_detail}</span>
                <button onClick={() => analyze(true)} style={{
                  marginLeft: 'auto', background: 'transparent',
                  border: '1px solid var(--border)', borderRadius: 4,
                  color: 'var(--text3)', fontSize: 10, padding: '2px 8px', cursor: 'pointer',
                }}>
                  Refresh
                </button>
              </div>

              {result.claude_analysis ? (
                <>
                  <pre style={{
                    whiteSpace: 'pre-wrap', wordBreak: 'break-word',
                    fontFamily: 'inherit', fontSize: 12,
                    color: 'var(--text2)', lineHeight: 1.6,
                    background: 'var(--surface)', borderRadius: 6,
                    padding: '10px 12px', margin: 0,
                  }}>
                    {result.claude_analysis}
                  </pre>
                  <RecommendationsTable recs={result.claude_recommendations} />
                </>
              ) : (
                <div style={{ color: 'var(--text3)', fontSize: 12, fontStyle: 'italic' }}>
                  Mechanical classification only. Set the ANTHROPIC_API_KEY environment variable
                  and click Refresh for AI-powered narrative and recommendations.
                </div>
              )}
            </>
          )}
        </div>
      </td>
    </tr>
  )
}

function LossTradesTable({ trades }) {
  const [expanded, setExpanded] = useState(null)

  if (!trades?.length) return (
    <div className="empty">No closed losing trades found.</div>
  )

  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Stock</th>
            <th>Strategy</th>
            <th>Category</th>
            <th>Closed</th>
            <th style={{ textAlign: 'right' }}>Duration</th>
            <th style={{ textAlign: 'right' }}>P&L</th>
            <th>Root Cause</th>
            <th style={{ textAlign: 'center' }}>Analysis</th>
          </tr>
        </thead>
        <tbody>
          {trades.map(t => {
            const isExpanded = expanded === t.id
            return (
              <>
                <tr key={t.id} style={{ background: isExpanded ? 'rgba(59,130,246,0.05)' : '' }}>
                  <td><span className="stock-code">{t.stock_code}</span></td>
                  <td style={{ color: 'var(--text2)', fontSize: 11 }}>{t.strategy}</td>
                  <td style={{ color: 'var(--text3)', fontSize: 11 }}>{t.category || '—'}</td>
                  <td style={{ color: 'var(--text3)', fontSize: 11 }}>
                    {t.closed_at ? String(t.closed_at).slice(0, 10) : '—'}
                  </td>
                  <td style={{ textAlign: 'right', color: 'var(--text3)', fontSize: 11 }}>
                    {t.duration_days != null ? `${t.duration_days}d` : '—'}
                  </td>
                  <td className="pnl-neg" style={{ textAlign: 'right' }}>
                    {t.close_pnl != null ? fmt(t.close_pnl) : '—'}
                  </td>
                  <td>
                    {t.root_cause
                      ? <RootCauseBadge cause={t.root_cause} />
                      : <span style={{ color: 'var(--text3)', fontSize: 11 }}>—</span>
                    }
                  </td>
                  <td style={{ textAlign: 'center' }}>
                    <button
                      onClick={() => setExpanded(isExpanded ? null : t.id)}
                      style={{
                        background: isExpanded ? 'var(--accent)' : 'var(--surface2)',
                        border: `1px solid ${isExpanded ? 'var(--accent)' : 'var(--border)'}`,
                        color: isExpanded ? '#fff' : 'var(--text2)',
                        borderRadius: 4, padding: '3px 10px',
                        fontSize: 10, cursor: 'pointer',
                      }}
                    >
                      {isExpanded ? 'Close' : 'Analyze'}
                    </button>
                  </td>
                </tr>
                {isExpanded && <AnalysisPanel key={`pm-${t.id}`} trade={t} onClose={() => setExpanded(null)} />}
              </>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function Insights() {
  const { data: breakdown, loading: bl } = useAutoRefresh('/api/postmortem/loss-breakdown', 60000)
  const { data: trades,    loading: tl } = useAutoRefresh('/api/postmortem/losses',         60000)

  return (
    <>
      {/* ── Loss Breakdown chart ─────────────────────────────────────────── */}
      <div className="chart-card" style={{ marginBottom: 16 }}>
        <h3>Loss Root Cause Breakdown</h3>
        {bl ? <div className="loading">Loading…</div> : <LossBreakdownChart data={breakdown} />}
      </div>

      {/* ── Weekly Insights panel ────────────────────────────────────────── */}
      <WeeklyInsights />

      {/* ── Losing trades table ──────────────────────────────────────────── */}
      <div className="table-card">
        <div className="table-toolbar">
          <span style={{ fontSize: 11, color: 'var(--text3)' }}>
            {trades?.length ?? 0} losing trades · click Analyze to classify
          </span>
        </div>
        {tl ? <div className="loading">Loading…</div> : <LossTradesTable trades={trades} />}
      </div>
    </>
  )
}
