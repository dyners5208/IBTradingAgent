import {
  BarChart, Bar, Cell, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer
} from 'recharts'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const fmt = v =>
  `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}`

function pct(v) {
  return v != null ? `${Math.round(v * 100)}%` : '—'
}

function pfLabel(pf) {
  if (pf === null || pf === undefined) return '∞'
  if (pf === 0) return '0.00'
  return Number(pf).toFixed(2)
}

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  const v = payload[0]?.value ?? 0
  return (
    <div style={{
      background: 'var(--surface2)', border: '1px solid var(--border2)',
      borderRadius: 6, padding: '10px 14px', fontSize: 12,
    }}>
      <div style={{ color: 'var(--text2)', marginBottom: 4, fontWeight: 600 }}>{label}</div>
      <div style={{ color: v >= 0 ? 'var(--green)' : 'var(--red)', fontWeight: 700 }}>
        P&L: {fmt(v)}
      </div>
    </div>
  )
}

export default function ByStrategy() {
  const { data, loading, error } = useAutoRefresh('/api/strategy-analytics')

  if (loading) return <div className="loading">Loading strategy data…</div>
  if (error)   return <div className="error">Error: {error}</div>
  if (!data?.length) return <div className="empty">No closed trades yet</div>

  const totalPnl  = data.reduce((acc, r) => acc + (r.total_pnl || 0), 0)
  const warnRows  = data.filter(r => r.flag === 'warn')
  const stopRows  = data.filter(r => r.flag === 'stop_rate')

  return (
    <>
      {/* ── Warning summary ─────────────────────────────────────────────── */}
      {(warnRows.length > 0 || stopRows.length > 0) && (
        <div style={{
          background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.25)',
          borderRadius: 8, padding: '10px 14px', marginBottom: 16, fontSize: 12,
          color: 'var(--red)',
        }}>
          <strong>Strategy Review Needed:</strong>{' '}
          {warnRows.length > 0 && (
            <span>
              {warnRows.map(r => r.strategy).join(', ')} — low win rate or profit factor below 1.0.{' '}
            </span>
          )}
          {stopRows.length > 0 && (
            <span>
              {stopRows.map(r => r.strategy).join(', ')} — stop rate &gt;50%.
            </span>
          )}
        </div>
      )}

      {/* ── Bar chart ────────────────────────────────────────────────────── */}
      <div className="chart-card">
        <h3>
          Total P&amp;L by Strategy
          <span style={{
            marginLeft: 12, fontSize: 13, fontWeight: 700,
            color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)',
          }}>
            {fmt(totalPnl)}
          </span>
        </h3>
        <ResponsiveContainer width="100%" height={Math.max(200, data.length * 42)}>
          <BarChart data={data} layout="vertical" margin={{ left: 10, right: 20, top: 4, bottom: 4 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" horizontal={false} />
            <XAxis type="number" tick={{ fontSize: 10, fill: 'var(--text3)' }}
                   tickLine={false} axisLine={false} tickFormatter={fmt} />
            <YAxis type="category" dataKey="strategy" tick={{ fontSize: 11, fill: 'var(--text2)' }}
                   tickLine={false} axisLine={false} width={140} />
            <Tooltip content={<CustomTooltip />} />
            <Bar dataKey="total_pnl" radius={[0, 4, 4, 0]} maxBarSize={28}>
              {data.map((entry, i) => (
                <Cell key={i}
                      fill={entry.total_pnl >= 0 ? '#10b981' : '#ef4444'}
                      fillOpacity={0.85} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {/* ── Analytics table ──────────────────────────────────────────────── */}
      <div className="table-card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Strategy</th>
                <th style={{ textAlign: 'right' }}>Trades</th>
                <th style={{ textAlign: 'right' }}>Win Rate</th>
                <th style={{ textAlign: 'right' }}>Profit Factor</th>
                <th style={{ textAlign: 'right' }}>Avg Days</th>
                <th style={{ textAlign: 'right' }}>TP Rate</th>
                <th style={{ textAlign: 'right' }}>Stop Rate</th>
                <th style={{ textAlign: 'right' }}>Avg P&amp;L</th>
                <th style={{ textAlign: 'right' }}>Total P&amp;L</th>
              </tr>
            </thead>
            <tbody>
              {data.map((row, i) => {
                const isWarn = row.flag === 'warn'
                const isStop = row.flag === 'stop_rate'
                const rowStyle = isWarn
                  ? { background: 'rgba(239,68,68,0.08)' }
                  : isStop
                  ? { background: 'rgba(245,158,11,0.08)' }
                  : {}

                const pfVal   = row.profit_factor != null ? pfLabel(row.profit_factor) : '∞'
                const pfCls   = row.profit_factor == null ? 'pnl-pos'
                              : row.profit_factor >= 1.5  ? 'pnl-pos'
                              : row.profit_factor >= 1.0  ? ''
                              : 'pnl-neg'
                const avgDays = row.avg_duration_days != null ? `${Math.round(row.avg_duration_days)}d` : '—'
                const stopCls = row.stop_rate != null && row.stop_rate > 0.5 ? 'pnl-neg' : ''

                return (
                  <tr key={i} style={rowStyle}>
                    <td style={{ fontWeight: 600 }}>
                      {row.strategy}
                      {isWarn && <span className="badge badge-loss" style={{ marginLeft: 6, fontSize: 9 }}>WARN</span>}
                      {isStop && <span className="badge badge-warning" style={{ marginLeft: 6, fontSize: 9 }}>STOPS</span>}
                    </td>
                    <td style={{ textAlign: 'right', color: 'var(--text2)' }}>{row.trades}</td>
                    <td style={{ textAlign: 'right' }}
                        className={row.win_rate >= 50 ? 'pnl-pos' : 'pnl-neg'}>
                      {row.win_rate != null ? `${row.win_rate}%` : '—'}
                    </td>
                    <td style={{ textAlign: 'right' }} className={pfCls}>{pfVal}</td>
                    <td style={{ textAlign: 'right', color: 'var(--text3)' }}>{avgDays}</td>
                    <td style={{ textAlign: 'right', color: 'var(--green)' }}>
                      {pct(row.tp_rate)}
                    </td>
                    <td style={{ textAlign: 'right' }} className={stopCls}>
                      {pct(row.stop_rate)}
                    </td>
                    <td style={{ textAlign: 'right' }}
                        className={row.avg_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                      {row.avg_pnl != null ? fmt(row.avg_pnl) : '—'}
                    </td>
                    <td style={{ textAlign: 'right' }}
                        className={row.total_pnl >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                      {fmt(row.total_pnl || 0)}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
