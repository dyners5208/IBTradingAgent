import { useMemo } from 'react'
import {
  AreaChart, Area, BarChart, Bar, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from 'recharts'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

// ── Formatters ────────────────────────────────────────────────────────────────

const fmt = v =>
  `$${Number(v || 0).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`

const fmtShort = v => {
  const n = Number(v || 0)
  if (Math.abs(n) >= 1000000) return `$${(n / 1000000).toFixed(1)}M`
  if (Math.abs(n) >= 1000)    return `$${(n / 1000).toFixed(1)}k`
  return `$${n.toFixed(0)}`
}

const fmtAxis = v => {
  const n = Number(v || 0)
  if (Math.abs(n) >= 1000) return `${n < 0 ? '-' : ''}$${(Math.abs(n) / 1000).toFixed(1)}k`
  return `$${n.toFixed(0)}`
}

// ── Badge helpers ─────────────────────────────────────────────────────────────

function profileBadge(trade) {
  const src = trade.scan_source
  if (src === 'russell')    return { label: 'RUSL',   bg: '#2e1065', color: '#c4b5fd' }
  if (src === 'politician') return { label: 'POL',    bg: '#052e16', color: '#6ee7b7' }
  if (src === 'gem')        return { label: 'GEM',    bg: '#083344', color: '#67e8f9' }
  const wt = trade.wheel_type
  if (wt === 'CSP' || wt === 'CC')
                            return { label: 'WHEEL',  bg: '#431407', color: '#fcd34d' }
  return                           { label: 'SPREAD', bg: '#172554', color: '#93c5fd' }
}

function signalStyle(signal) {
  if (signal === 'BUY')  return { bg: '#052e16', color: '#34d399', dot: '#10b981' }
  if (signal === 'SELL') return { bg: '#450a0a', color: '#f87171', dot: '#ef4444' }
  return                        { bg: '#1c1410', color: '#fbbf24', dot: '#f59e0b' }
}

// ── Sub-components ────────────────────────────────────────────────────────────

function StatCard({ label, value, sub, valueClass }) {
  return (
    <div className="cc-stat-card">
      <div className="cc-stat-label">{label}</div>
      <div className={`cc-stat-value ${valueClass || ''}`}>{value}</div>
      {sub && <div className="cc-stat-sub">{sub}</div>}
    </div>
  )
}

function BudgetBar({ label, deployed, budget, pct }) {
  const fill    = Math.min(pct || 0, 100)
  const barColor = fill > 90 ? '#ef4444' : fill > 70 ? '#f59e0b' : '#3b82f6'
  return (
    <div className="cc-budget-row">
      <div className="cc-budget-label">
        <span>{label}</span>
        <span className="cc-budget-nums">{fmtShort(deployed)} / {fmtShort(budget)}</span>
      </div>
      <div className="cc-budget-track">
        <div className="cc-budget-fill" style={{ width: `${fill}%`, background: barColor }} />
      </div>
    </div>
  )
}

// ── Main component ────────────────────────────────────────────────────────────

export default function Overview() {
  const { data: stats }        = useAutoRefresh('/api/stats',          30000)
  const { data: account }      = useAutoRefresh('/api/account',        30000)
  const { data: openTrades }   = useAutoRefresh('/api/trades/open',    30000)
  const { data: livePosRaw }   = useAutoRefresh('/api/positions-live', 30000)
  const { data: signals }      = useAutoRefresh('/api/signals',        60000)
  const { data: budget }       = useAutoRefresh('/api/budget',         60000)
  const { data: pnlHistory }   = useAutoRefresh('/api/pnl-history',   120000)
  const { data: pnlByTicker }  = useAutoRefresh('/api/pnl-by-ticker', 120000)
  const { data: todayActivity } = useAutoRefresh('/api/today-activity', 30000)

  // Live positions keyed by symbol
  const livePosMap = useMemo(() => {
    const map = {}
    for (const p of (livePosRaw || [])) map[p.symbol] = p
    return map
  }, [livePosRaw])

  // Daily realized losses from today's closed trades
  const dailyLoss = useMemo(() =>
    (todayActivity?.closed || []).reduce((sum, t) => {
      const pnl = t.close_pnl_usd || 0
      return pnl < 0 ? sum + Math.abs(pnl) : sum
    }, 0)
  , [todayActivity])

  const DAILY_LOSS_LIMIT = 1000

  // Stats bar values
  const equity       = account?.total_equity
  const cash         = account?.cash
  const isPaper      = account?.is_paper !== false
  const openCount    = stats?.open_count   || 0
  const maxPos       = budget?.max_positions || 10
  const winRate      = stats?.win_rate     || 0
  const winCount     = stats?.win_count    || 0
  const lossCount    = Math.max(0, (stats?.closed_count || 0) - winCount)
  const todayPnl     = stats?.today_pnl    || 0
  const totalPnl     = stats?.total_pnl    || 0
  const startEquity  = equity != null ? equity - totalPnl : null

  // Chart data — last 30 days
  const chartData    = (pnlHistory?.daily || []).slice(-30)
  const trades       = openTrades || []
  const sigLatest    = (signals?.latest || []).slice(0, 7)

  return (
    <div className="cc-wrapper">

      {/* ── Stats bar ── */}
      <div className="cc-stats-bar">
        <StatCard
          label="TOTAL EQUITY"
          value={equity != null ? fmt(equity) : '—'}
          sub={startEquity != null ? `Started ${fmt(startEquity)}` : isPaper ? 'Paper account' : 'Connecting…'}
        />
        <StatCard
          label="TODAY'S P&L"
          value={fmt(todayPnl)}
          sub={equity ? `${(todayPnl / equity * 100).toFixed(2)}%` : null}
          valueClass={todayPnl >= 0 ? 'cc-pos' : 'cc-neg'}
        />
        <StatCard
          label="TOTAL RETURN"
          value={fmt(totalPnl)}
          sub={startEquity ? `${(totalPnl / startEquity * 100).toFixed(2)}% all-time` : null}
          valueClass={totalPnl >= 0 ? 'cc-pos' : 'cc-neg'}
        />
        <StatCard
          label="CASH AVAILABLE"
          value={cash != null ? fmt(cash) : '—'}
          sub={equity && cash ? `${(cash / equity * 100).toFixed(0)}% of equity` : null}
        />
        <StatCard
          label="POSITIONS"
          value={`${openCount} / ${maxPos}`}
          sub={`${maxPos - openCount} slot${maxPos - openCount !== 1 ? 's' : ''} free · scanning`}
        />
        <StatCard
          label="WIN RATE"
          value={`${winRate}%`}
          sub={`${winCount}W · ${lossCount}L`}
          valueClass={winRate >= 55 ? 'cc-pos' : winRate < 35 ? 'cc-neg' : ''}
        />
      </div>

      {/* ── Main grid: positions + sidebar ── */}
      <div className="cc-main">

        {/* Left: open positions table */}
        <div className="cc-card cc-positions-card">
          <div className="cc-card-header">
            <span>OPEN POSITIONS ({openCount} / {maxPos})</span>
            {openCount < maxPos && (
              <span className="cc-header-badge cc-badge-green">
                {maxPos - openCount} slot{maxPos - openCount !== 1 ? 's' : ''} free
              </span>
            )}
          </div>
          <div className="cc-table-scroll">
            <table className="cc-table">
              <thead>
                <tr>
                  <th>TICKER</th>
                  <th>PROFILE</th>
                  <th>QTY</th>
                  <th>ENTRY</th>
                  <th>CURRENT</th>
                  <th>MKT VALUE</th>
                  <th>UNREALIZED P&L</th>
                  <th>TAKE PROFIT</th>
                  <th>STOP LOSS</th>
                </tr>
              </thead>
              <tbody>
                {trades.length === 0 && (
                  <tr>
                    <td colSpan="9" className="cc-empty-cell">No open positions</td>
                  </tr>
                )}
                {trades.map((t, i) => {
                  const { label: profLabel, bg: profBg, color: profColor } = profileBadge(t)
                  const ticker  = (t.stock_code || '').replace(/^(US\.|HK\.)/, '')
                  const live    = livePosMap[ticker]

                  const qty = t.trade_type === 'stock'
                    ? (t.qty != null ? t.qty : '—')
                    : (t.num_contracts != null ? `${t.num_contracts}c` : '—')

                  const entry = t.trade_type === 'stock'
                    ? (t.limit_price != null ? `$${Number(t.limit_price).toFixed(2)}` : '—')
                    : (t.net_credit_per_spread != null ? `$${Number(t.net_credit_per_spread).toFixed(2)}` : '—')

                  const current  = live?.current_price   != null ? `$${Number(live.current_price).toFixed(2)}`  : '—'
                  const mktVal   = live?.market_value     != null ? fmt(live.market_value)                        : '—'
                  const unreal   = live?.unrealized_pl    != null ? live.unrealized_pl  : (t.unrealized_pnl ?? null)
                  const unrealPc = live?.unrealized_plpc  != null ? live.unrealized_plpc * 100 : null
                  const uCls     = (unreal ?? 0) >= 0 ? 'cc-pos' : 'cc-neg'

                  return (
                    <tr key={i}>
                      <td><span className="cc-ticker-lbl">{ticker}</span></td>
                      <td>
                        <span className="cc-badge" style={{ background: profBg, color: profColor }}>
                          {profLabel}
                        </span>
                      </td>
                      <td className="cc-mono">{qty}</td>
                      <td className="cc-mono">{entry}</td>
                      <td className="cc-mono">{current}</td>
                      <td className="cc-mono">{mktVal}</td>
                      <td className={`cc-mono ${uCls}`}>
                        {unreal != null ? fmt(unreal) : '—'}
                        {unrealPc != null && (
                          <span className="cc-pct-tag">
                            {' '}({unrealPc >= 0 ? '+' : ''}{unrealPc.toFixed(2)}%)
                          </span>
                        )}
                      </td>
                      <td className="cc-mono cc-pos">{t.tp_value != null ? fmt(t.tp_value) : '—'}</td>
                      <td className="cc-mono cc-neg">{t.cl_value != null ? fmt(t.cl_value) : '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        </div>

        {/* Right: sidebar */}
        <div className="cc-sidebar">

          {/* Risk monitor */}
          <div className="cc-card cc-risk-card">
            <div className="cc-card-header">
              <span>RISK MONITOR</span>
              <span className="cc-header-badge cc-badge-green">All clear</span>
            </div>
            <div className="cc-risk-body">
              {/* Daily loss bar */}
              <div className="cc-budget-row">
                <div className="cc-budget-label">
                  <span>Daily Loss Used</span>
                  <span className={`cc-budget-nums ${dailyLoss > 0 ? 'cc-neg' : ''}`}>
                    {fmtShort(dailyLoss)} / {fmtShort(DAILY_LOSS_LIMIT)}
                  </span>
                </div>
                <div className="cc-budget-track">
                  <div
                    className="cc-budget-fill"
                    style={{
                      width:      `${Math.min(dailyLoss / DAILY_LOSS_LIMIT * 100, 100)}%`,
                      background: dailyLoss / DAILY_LOSS_LIMIT > 0.8 ? '#ef4444' : '#3b82f6',
                    }}
                  />
                </div>
              </div>
              {/* Positions bar */}
              <div className="cc-budget-row">
                <div className="cc-budget-label">
                  <span>Positions Open</span>
                  <span className="cc-budget-nums">{openCount} / {maxPos}</span>
                </div>
                <div className="cc-budget-track">
                  <div
                    className="cc-budget-fill"
                    style={{
                      width:      `${maxPos ? openCount / maxPos * 100 : 0}%`,
                      background: '#3b82f6',
                    }}
                  />
                </div>
              </div>
              {/* Category budget bars */}
              {(budget?.categories || []).map(cat => (
                <BudgetBar
                  key={cat.name}
                  label={cat.name}
                  deployed={cat.deployed}
                  budget={cat.budget}
                  pct={cat.pct}
                />
              ))}
              {equity != null && cash != null && (
                <div className="cc-risk-deployed">
                  Capital Deployed
                  <span className="cc-risk-deployed-val">{fmt(equity - cash)}</span>
                </div>
              )}
            </div>
          </div>

          {/* Signal feed */}
          <div className="cc-card cc-signals-card">
            <div className="cc-card-header">
              <span>LATEST SIGNALS</span>
              {signals?.scan_date && (
                <span className="cc-header-meta">{signals.scan_date}</span>
              )}
            </div>
            <div className="cc-signal-list">
              {sigLatest.length === 0
                ? <div className="cc-empty-cell" style={{ padding: '10px 0' }}>No signals yet today</div>
                : sigLatest.map((s, i) => {
                    const { bg, color, dot } = signalStyle(s.signal)
                    return (
                      <div key={i} className="cc-signal-row">
                        <span className="cc-sig-ticker">{s.stock_code}</span>
                        <span className="cc-sig-badge" style={{ background: bg, color }}>
                          <span className="cc-dot" style={{ background: dot }} />
                          {s.signal}
                        </span>
                        <span className="cc-sig-score" style={{ color }}>
                          {(Math.abs(s.score) * 100).toFixed(0)}%
                        </span>
                        <span className="cc-sig-strat">
                          {(s.strategy || '').split(' ').slice(0, 2).join(' ')}
                        </span>
                      </div>
                    )
                  })
              }
            </div>
          </div>

        </div>
      </div>

      {/* ── Bottom row ── */}
      <div className="cc-bottom">

        {/* Cumulative P&L chart */}
        <div className="cc-card">
          <div className="cc-card-header">CUMULATIVE P&L</div>
          <ResponsiveContainer width="100%" height={150}>
            <AreaChart data={chartData} margin={{ top: 6, right: 8, left: 0, bottom: 0 }}>
              <defs>
                <linearGradient id="ccPnlGrad" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="5%"  stopColor={totalPnl >= 0 ? '#10b981' : '#ef4444'} stopOpacity={0.35} />
                  <stop offset="95%" stopColor={totalPnl >= 0 ? '#10b981' : '#ef4444'} stopOpacity={0}    />
                </linearGradient>
              </defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#1c2640" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 9, fill: '#4b5563' }}
                tickFormatter={d => d.slice(5)}
                interval="preserveStartEnd"
              />
              <YAxis
                tick={{ fontSize: 9, fill: '#4b5563' }}
                width={48}
                tickFormatter={fmtAxis}
              />
              <Tooltip
                contentStyle={{ background: '#0f1623', border: '1px solid #1c2640', fontSize: 11 }}
                formatter={v => [fmt(v), 'Cumulative']}
              />
              <Area
                type="monotone"
                dataKey="cumulative"
                stroke={totalPnl >= 0 ? '#10b981' : '#ef4444'}
                fill="url(#ccPnlGrad)"
                strokeWidth={1.5}
                dot={false}
              />
            </AreaChart>
          </ResponsiveContainer>
        </div>

        {/* P&L by Ticker */}
        <div className="cc-card">
          <div className="cc-card-header">P&L BY TICKER</div>
          {(pnlByTicker || []).length === 0
            ? <div className="cc-empty-cell" style={{ height: 150, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>No closed trades yet</div>
            : (
              <ResponsiveContainer width="100%" height={150}>
                <BarChart
                  data={(pnlByTicker || []).slice(0, 8)}
                  layout="vertical"
                  margin={{ top: 4, right: 12, left: 4, bottom: 0 }}
                >
                  <CartesianGrid strokeDasharray="3 3" stroke="#1c2640" horizontal={false} />
                  <XAxis
                    type="number"
                    tick={{ fontSize: 9, fill: '#4b5563' }}
                    tickFormatter={fmtAxis}
                  />
                  <YAxis
                    type="category"
                    dataKey="stock_code"
                    tick={{ fontSize: 9, fill: '#9ca3af' }}
                    width={40}
                  />
                  <Tooltip
                    contentStyle={{ background: '#0f1623', border: '1px solid #1c2640', fontSize: 11 }}
                    formatter={v => [fmt(v), 'P&L']}
                  />
                  <Bar dataKey="pnl" radius={[0, 3, 3, 0]}>
                    {(pnlByTicker || []).slice(0, 8).map((e, idx) => (
                      <Cell key={idx} fill={e.pnl >= 0 ? '#10b981' : '#ef4444'} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            )
          }
          {/* Win/Loss summary */}
          {(pnlByTicker || []).length > 0 && (
            <div className="cc-ticker-summary">
              <span>
                AVG WIN <span className="cc-pos">
                  {fmt((pnlByTicker || []).filter(x => x.pnl > 0).reduce((s, x) => s + x.pnl, 0)
                    / Math.max(1, (pnlByTicker || []).filter(x => x.pnl > 0).length))}
                </span>
              </span>
              <span>
                AVG LOSS <span className="cc-neg">
                  {fmt((pnlByTicker || []).filter(x => x.pnl < 0).reduce((s, x) => s + x.pnl, 0)
                    / Math.max(1, (pnlByTicker || []).filter(x => x.pnl < 0).length))}
                </span>
              </span>
            </div>
          )}
        </div>

        {/* Signal distribution */}
        <div className="cc-card">
          <div className="cc-card-header">
            SIGNAL DISTRIBUTION
            {signals?.total_scanned ? (
              <span className="cc-header-meta">{signals.total_scanned.toLocaleString()} total</span>
            ) : null}
          </div>
          <div className="cc-dist-body">
            {[
              { label: 'BUY',  count: signals?.buy  || 0, color: '#10b981' },
              { label: 'HOLD', count: signals?.hold || 0, color: '#f59e0b' },
              { label: 'SELL', count: signals?.sell  || 0, color: '#ef4444' },
            ].map(({ label, count, color }) => {
              const total = (signals?.buy || 0) + (signals?.hold || 0) + (signals?.sell || 0)
              const pct   = total ? (count / total * 100) : 0
              return (
                <div key={label} className="cc-dist-row">
                  <span className="cc-dist-label" style={{ color }}>{label}</span>
                  <div className="cc-dist-track">
                    <div className="cc-dist-fill" style={{ width: `${pct}%`, background: color }} />
                  </div>
                  <span className="cc-dist-count">{count}</span>
                </div>
              )
            })}
          </div>
          <div className="cc-dist-footer">
            {signals?.scan_date && (
              <div>Scanned {signals.scan_date}</div>
            )}
            {signals?.executed != null && (
              <div>
                <span className="cc-pos">{signals.executed}</span>
                {' / '}
                <span>{signals.buy || 0}</span>
                {' BUY signals executed'}
              </div>
            )}
            {!signals?.scan_date && (
              <div style={{ color: '#4b5563' }}>Awaiting scan…</div>
            )}
          </div>
        </div>

      </div>
    </div>
  )
}
