import { useAutoRefresh } from '../hooks/useAutoRefresh'

const fmt2 = v =>
  v != null ? `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '—'

export default function WheelTracker() {
  const { data, loading, error } = useAutoRefresh('/api/wheel')

  if (loading) return <div className="loading">Loading wheel data…</div>
  if (error)   return <div className="error">Error: {error}</div>
  if (!data?.length) return <div className="empty">No wheel trades found</div>

  const totalPnl = data.reduce((acc, r) => acc + (r.total_pnl || 0), 0)

  return (
    <>
      <div className="cards-row" style={{ marginBottom: 16 }}>
        <div className="card">
          <div className="label">Wheel Positions</div>
          <div className="value accent">{data.length}</div>
        </div>
        <div className="card">
          <div className="label">Active</div>
          <div className="value accent">{data.filter(r => r.status === 'open').length}</div>
        </div>
        <div className="card amber-top">
          <div className="label">Total Wheel P&L</div>
          <div className={`value ${totalPnl >= 0 ? 'green' : 'red'}`}>{fmt2(totalPnl)}</div>
        </div>
        <div className="card green-top">
          <div className="label">Total Premiums</div>
          <div className="value green">
            {fmt2(data.reduce((acc, r) => acc + (r.premiums_collected || 0), 0))}
          </div>
        </div>
      </div>

      <div className="table-card">
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Stock</th>
                <th style={{ textAlign: 'right' }}>Assignment</th>
                <th style={{ textAlign: 'right' }}>Premiums</th>
                <th style={{ textAlign: 'right' }}>Net Basis/sh</th>
                <th style={{ textAlign: 'center' }}>CSPs</th>
                <th style={{ textAlign: 'center' }}>CCs</th>
                <th style={{ textAlign: 'right' }}>Realized P&amp;L</th>
                <th style={{ textAlign: 'center' }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {data.map((r, i) => (
                <tr key={i}>
                  <td><span className="stock-code">{r.stock_code}</span></td>
                  <td style={{ textAlign: 'right', color: 'var(--text2)' }}>
                    {r.assignment_price != null ? `$${Number(r.assignment_price).toFixed(2)}` : '—'}
                  </td>
                  <td style={{ textAlign: 'right' }} className="pnl-pos">
                    {fmt2(r.premiums_collected)}
                  </td>
                  <td style={{ textAlign: 'right', fontFamily: 'monospace' }}>
                    {r.net_cost_basis != null
                      ? <span style={{ color: 'var(--amber)' }}>${Number(r.net_cost_basis).toFixed(2)}</span>
                      : '—'}
                  </td>
                  <td style={{ textAlign: 'center', color: 'var(--text2)' }}>{r.csp_count}</td>
                  <td style={{ textAlign: 'center', color: 'var(--text2)' }}>{r.cc_count}</td>
                  <td style={{ textAlign: 'right' }}
                      className={(r.total_pnl ?? 0) >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                    {fmt2(r.total_pnl)}
                  </td>
                  <td style={{ textAlign: 'center' }}>
                    <span className={`badge ${r.status === 'open' ? 'badge-open' : 'badge-closed'}`}>
                      {r.status}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </>
  )
}
