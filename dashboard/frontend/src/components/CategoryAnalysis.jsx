import { useState } from 'react'
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceLine
} from 'recharts'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const CATEGORIES = ['US Spreads', 'Russell', 'US Politician', 'Wheel', 'HK Stocks']

const CAT_COLORS = {
  'US Spreads':    '#3b82f6',
  'Russell':       '#8b5cf6',
  'US Politician': '#10b981',
  'Wheel':         '#f59e0b',
  'HK Stocks':     '#ef4444',
}

const fmt = v =>
  `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

function pfLabel(pf) {
  if (pf === null) return '∞'
  if (pf === 0)    return '0.00'
  return pf.toFixed(2)
}

function pfColor(pf) {
  if (pf === null) return 'green'     // infinite = all wins
  if (pf >= 1.5)   return 'green'
  if (pf >= 1.0)   return 'amber'
  return 'red'
}

function ScoreCard({ label, value, colorClass, sub }) {
  return (
    <div className="card" style={{ minWidth: 130 }}>
      <div className="label">{label}</div>
      <div className={`value ${colorClass || ''}`} style={{ fontSize: 20 }}>{value}</div>
      {sub && <div className="sub-label">{sub}</div>}
    </div>
  )
}

function CumulativeChart({ trades, color }) {
  const closed = trades
    .filter(t => t.status === 'closed' && t.closed_at)
    .sort((a, b) => (a.closed_at > b.closed_at ? 1 : -1))

  if (!closed.length) return <div className="empty">No closed trade history for this category</div>

  let cum = 0
  const data = closed.map(t => {
    cum += t.close_pnl || 0
    return { date: String(t.closed_at).slice(0, 10), pnl: t.close_pnl || 0, cumulative: Math.round(cum * 100) / 100 }
  })

  const gradId = `catGrad_${color.replace('#', '')}`

  return (
    <ResponsiveContainer width="100%" height={200}>
      <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 10 }}>
        <defs>
          <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="5%"  stopColor={color} stopOpacity={0.2} />
            <stop offset="95%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
        <XAxis dataKey="date" tick={{ fontSize: 9, fill: 'var(--text3)' }} tickLine={false} axisLine={false} />
        <YAxis tick={{ fontSize: 9, fill: 'var(--text3)' }} tickLine={false} axisLine={false}
               tickFormatter={v => `$${v}`} width={48} />
        <Tooltip formatter={v => [fmt(v), 'Cumulative P&L']}
                 contentStyle={{ background: 'var(--surface2)', border: '1px solid var(--border2)', borderRadius: 6, fontSize: 12 }} />
        <ReferenceLine y={0} stroke="var(--border2)" strokeDasharray="4 2" />
        <Area type="monotone" dataKey="cumulative"
              stroke={color} fill={`url(#${gradId})`} strokeWidth={2} dot={false} />
      </AreaChart>
    </ResponsiveContainer>
  )
}

function dteCls(dte) {
  if (dte == null) return ''
  if (dte <= 7)    return 'dte-urgent'
  if (dte <= 14)   return 'dte-warn'
  return 'dte-ok'
}

export default function CategoryAnalysis() {
  const [selected, setSelected] = useState('US Spreads')
  const encodedCat = encodeURIComponent(selected)
  const { data: trades, loading, error, lastUpdated, refresh } =
    useAutoRefresh(`/api/category-trades?category=${encodedCat}`)

  const open   = trades?.filter(t => t.status === 'open')   || []
  const closed = trades?.filter(t => t.status === 'closed') || []

  // Compute scorecard metrics from closed trades (excluding cancelled)
  const realClosed = closed.filter(t => t.close_reason !== 'cancelled')
  const wins       = realClosed.filter(t => (t.close_pnl || 0) > 0)
  const losses     = realClosed.filter(t => (t.close_pnl || 0) < 0)
  const winRate    = realClosed.length ? Math.round(wins.length / realClosed.length * 100) : null
  const winsSum    = wins.reduce((s, t) => s + (t.close_pnl || 0), 0)
  const lossSum    = Math.abs(losses.reduce((s, t) => s + (t.close_pnl || 0), 0))
  const pf         = winsSum > 0 && lossSum > 0 ? winsSum / lossSum : (winsSum > 0 ? null : 0)
  const avgWin     = wins.length   ? winsSum / wins.length   : null
  const avgLoss    = losses.length ? -lossSum / losses.length : null
  const durations  = realClosed.filter(t => t.duration_days != null).map(t => t.duration_days)
  const avgDur     = durations.length ? Math.round(durations.reduce((s, v) => s + v, 0) / durations.length) : null

  const byPnl  = [...realClosed].sort((a, b) => (b.close_pnl || 0) - (a.close_pnl || 0))
  const best   = byPnl[0]
  const worst  = byPnl[byPnl.length - 1]

  const color = CAT_COLORS[selected] || '#3b82f6'

  return (
    <>
      {/* ── Category selector ────────────────────────────────────────────── */}
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 18 }}>
        {CATEGORIES.map(cat => (
          <button
            key={cat}
            onClick={() => setSelected(cat)}
            style={{
              padding: '7px 14px',
              borderRadius: 20,
              border: `1px solid ${selected === cat ? CAT_COLORS[cat] : 'var(--border)'}`,
              background: selected === cat ? `${CAT_COLORS[cat]}22` : 'var(--surface)',
              color:  selected === cat ? CAT_COLORS[cat] : 'var(--text2)',
              cursor: 'pointer',
              fontSize: 12,
              fontWeight: selected === cat ? 700 : 400,
              transition: 'all 0.15s',
            }}
          >
            {cat}
          </button>
        ))}
      </div>

      {loading && <div className="loading">Loading…</div>}
      {error   && <div className="error">Error: {error}</div>}

      {!loading && !error && (
        <>
          {/* ── Scorecard ────────────────────────────────────────────────── */}
          <div className="cards-row">
            <ScoreCard label="Win Rate"
                       value={winRate != null ? `${winRate}%` : '—'}
                       colorClass={winRate == null ? '' : winRate >= 50 ? 'green' : 'red'} />
            <ScoreCard label="Profit Factor"
                       value={pfLabel(pf)}
                       colorClass={pfColor(pf)}
                       sub="win$ / loss$" />
            <ScoreCard label="Avg Win"
                       value={avgWin != null ? fmt(avgWin) : '—'}
                       colorClass="green" />
            <ScoreCard label="Avg Loss"
                       value={avgLoss != null ? `−${fmt(avgLoss)}` : '—'}
                       colorClass="red" />
            <ScoreCard label="Avg Duration"
                       value={avgDur != null ? `${avgDur}d` : '—'}
                       colorClass="" />
          </div>

          {/* ── Best / Worst ─────────────────────────────────────────────── */}
          {(best || worst) && (
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12, marginBottom: 16 }}>
              {best && (
                <div className="card green-top">
                  <div className="label">Best Trade</div>
                  <div style={{ fontWeight: 700, fontSize: 13, color: 'var(--text)', marginBottom: 2 }}>
                    {best.stock_code}
                  </div>
                  <div className="value green" style={{ fontSize: 18 }}>{fmt(best.close_pnl)}</div>
                  <div className="sub-label">{best.strategy} · {best.duration_days != null ? `${best.duration_days}d` : ''}</div>
                </div>
              )}
              {worst && worst !== best && (
                <div className="card red-top">
                  <div className="label">Worst Trade</div>
                  <div style={{ fontWeight: 700, fontSize: 13, color: 'var(--text)', marginBottom: 2 }}>
                    {worst.stock_code}
                  </div>
                  <div className="value red" style={{ fontSize: 18 }}>{fmt(worst.close_pnl)}</div>
                  <div className="sub-label">{worst.strategy} · {worst.duration_days != null ? `${worst.duration_days}d` : ''}</div>
                </div>
              )}
            </div>
          )}

          {/* ── Cumulative P&L chart ─────────────────────────────────────── */}
          <div className="chart-card" style={{ marginBottom: 16 }}>
            <h3>{selected} — Cumulative P&L</h3>
            <CumulativeChart trades={trades || []} color={color} />
          </div>

          {/* ── Trade table ──────────────────────────────────────────────── */}
          <div className="table-card">
            <div className="table-toolbar">
              <span style={{ fontSize: 11, color: 'var(--text3)' }}>
                {open.length} open · {realClosed.length} closed
              </span>
              {lastUpdated && <span className="last-updated">Updated {lastUpdated}</span>}
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Stock</th>
                    <th>Strategy</th>
                    <th>Status</th>
                    <th>Expiry</th>
                    <th>DTE</th>
                    <th style={{ textAlign: 'right' }}>Duration</th>
                    <th style={{ textAlign: 'right' }}>Income</th>
                    <th style={{ textAlign: 'right' }}>P&L</th>
                    <th style={{ textAlign: 'right' }}>ROI%</th>
                    <th>Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {(trades || []).map((t, i) => {
                    const pnl = t.close_pnl ?? null
                    const isOpen = t.status === 'open'
                    return (
                      <tr key={i} style={isOpen ? { background: 'rgba(59,130,246,0.04)' } : {}}>
                        <td><span className="stock-code">{t.stock_code}</span></td>
                        <td style={{ color: 'var(--text2)', fontSize: 11 }}>{t.strategy}</td>
                        <td>
                          <span className={`badge ${isOpen ? 'badge-open' : 'badge-closed'}`}>
                            {t.status}
                          </span>
                        </td>
                        <td style={{ color: 'var(--text3)', fontSize: 11 }}>
                          {t.exp_date ? String(t.exp_date).slice(0, 10) : '—'}
                        </td>
                        <td className={dteCls(t.dte)}>
                          {t.dte != null ? `${t.dte}d` : '—'}
                        </td>
                        <td style={{ textAlign: 'right', color: 'var(--text3)' }}>
                          {t.duration_days != null ? `${t.duration_days}d` : '—'}
                        </td>
                        <td style={{ textAlign: 'right', color: 'var(--text2)', fontFamily: 'monospace', fontSize: 11 }}>
                          {t.income_usd != null ? fmt(t.income_usd) : '—'}
                        </td>
                        <td style={{ textAlign: 'right' }}
                            className={pnl == null ? '' : pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : 'pnl-zero'}>
                          {pnl != null ? fmt(pnl) : '—'}
                        </td>
                        <td style={{ textAlign: 'right' }}
                            className={t.roi_pct == null ? '' : t.roi_pct > 0 ? 'pnl-pos' : 'pnl-neg'}>
                          {t.roi_pct != null ? `${t.roi_pct}%` : '—'}
                        </td>
                        <td style={{ color: 'var(--text3)', fontSize: 11 }}>
                          {t.close_reason || '—'}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {(!trades || !trades.length) && (
                <div className="empty">No trades in this category yet</div>
              )}
            </div>
          </div>
        </>
      )}
    </>
  )
}
