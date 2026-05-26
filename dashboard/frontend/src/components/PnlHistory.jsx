import {
  AreaChart, Area, BarChart, Bar, Cell,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, ReferenceLine
} from 'recharts'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const fmt = v =>
  `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}`

const CustomTooltip = ({ active, payload, label }) => {
  if (!active || !payload?.length) return null
  return (
    <div style={{
      background: 'var(--surface2)', border: '1px solid var(--border2)',
      borderRadius: 6, padding: '10px 14px', fontSize: 12,
    }}>
      <div style={{ color: 'var(--text3)', marginBottom: 6 }}>{label}</div>
      {payload.map(p => (
        <div key={p.dataKey} style={{ color: p.value >= 0 ? 'var(--green)' : 'var(--red)', fontWeight: 600 }}>
          {p.dataKey === 'cumulative' ? 'Cumulative' : 'Daily'}: {fmt(p.value)}
        </div>
      ))}
    </div>
  )
}

export default function PnlHistory() {
  const { data, loading, error } = useAutoRefresh('/api/pnl-history')

  if (loading) return <div className="loading">Loading P&L history…</div>
  if (error)   return <div className="error">Error: {error}</div>
  if (!data?.daily?.length) return <div className="empty">No closed trade history yet</div>

  const totalPnl = data.daily[data.daily.length - 1]?.cumulative ?? 0

  return (
    <>
      <div className="chart-card">
        <h3>
          Cumulative P&amp;L
          <span style={{
            marginLeft: 12, fontSize: 14, fontWeight: 700,
            color: totalPnl >= 0 ? 'var(--green)' : 'var(--red)',
          }}>
            {fmt(totalPnl)}
          </span>
        </h3>
        <ResponsiveContainer width="100%" height={300}>
          <AreaChart data={data.daily} margin={{ top: 4, right: 8, bottom: 0, left: 10 }}>
            <defs>
              <linearGradient id="cumGradPos" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%"  stopColor="#10b981" stopOpacity={0.2} />
                <stop offset="95%" stopColor="#10b981" stopOpacity={0} />
              </linearGradient>
              <linearGradient id="cumGradNeg" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%"  stopColor="#ef4444" stopOpacity={0.2} />
                <stop offset="95%" stopColor="#ef4444" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'var(--text3)' }} tickLine={false} axisLine={false} />
            <YAxis tick={{ fontSize: 10, fill: 'var(--text3)' }} tickLine={false} axisLine={false}
                   tickFormatter={v => `$${(v/1000).toFixed(0)}k`} width={48} />
            <Tooltip content={<CustomTooltip />} />
            <ReferenceLine y={0} stroke="var(--border2)" strokeDasharray="4 2" />
            <Area type="monotone" dataKey="cumulative"
                  stroke={totalPnl >= 0 ? '#10b981' : '#ef4444'}
                  fill={totalPnl >= 0 ? 'url(#cumGradPos)' : 'url(#cumGradNeg)'}
                  strokeWidth={2} dot={false} />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <div className="chart-card">
        <h3>Daily P&amp;L</h3>
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={data.daily} margin={{ top: 4, right: 8, bottom: 0, left: 10 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
            <XAxis dataKey="date" tick={{ fontSize: 10, fill: 'var(--text3)' }} tickLine={false} axisLine={false} />
            <YAxis tick={{ fontSize: 10, fill: 'var(--text3)' }} tickLine={false} axisLine={false}
                   tickFormatter={v => `$${v}`} width={52} />
            <Tooltip content={<CustomTooltip />} />
            <ReferenceLine y={0} stroke="var(--border2)" />
            <Bar dataKey="pnl" radius={[3, 3, 0, 0]} maxBarSize={40}>
              {data.daily.map((entry, i) => (
                <Cell key={i} fill={entry.pnl >= 0 ? '#10b981' : '#ef4444'}
                      fillOpacity={0.85} />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>

      {data.monthly?.length > 0 && (
        <div className="chart-card">
          <h3>Monthly P&amp;L</h3>
          <ResponsiveContainer width="100%" height={240}>
            <BarChart data={data.monthly} margin={{ top: 4, right: 8, bottom: 0, left: 10 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="var(--border)" />
              <XAxis dataKey="month" tick={{ fontSize: 10, fill: 'var(--text3)' }} tickLine={false} axisLine={false} />
              <YAxis tick={{ fontSize: 10, fill: 'var(--text3)' }} tickLine={false} axisLine={false}
                     tickFormatter={v => `$${v}`} width={52} />
              <Tooltip content={<CustomTooltip />} />
              <ReferenceLine y={0} stroke="var(--border2)" />
              <Bar dataKey="pnl" radius={[4, 4, 0, 0]} maxBarSize={60}>
                {data.monthly.map((entry, i) => (
                  <Cell key={i} fill={entry.pnl >= 0 ? '#10b981' : '#ef4444'}
                        fillOpacity={0.85} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </>
  )
}
