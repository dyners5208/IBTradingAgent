import { useState } from 'react'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

function dteCls(dte) {
  if (dte == null) return ''
  if (dte <= 7)  return 'dte-urgent'
  if (dte <= 14) return 'dte-warn'
  return 'dte-ok'
}

function sourceBadge(src, wt) {
  if (wt === 'CSP' || wt === 'CC') return <span className="badge badge-source">{wt}</span>
  if (src === 'russell')            return <span className="badge badge-source" style={{ color: 'var(--purple)', borderColor: 'rgba(139,92,246,0.3)', background: 'rgba(139,92,246,0.1)' }}>RUSSELL</span>
  if (src === 'politician')         return <span className="badge badge-source" style={{ color: 'var(--green)', borderColor: 'rgba(16,185,129,0.3)', background: 'var(--green-dim)' }}>POL</span>
  return null
}

export default function OpenPositions() {
  const { data, loading, error, lastUpdated } = useAutoRefresh('/api/trades/open')
  const [search, setSearch]   = useState('')
  const [sortKey, setSortKey] = useState('opened_at')
  const [sortAsc, setSortAsc] = useState(false)

  if (loading) return <div className="loading">Loading positions…</div>
  if (error)   return <div className="error">Error: {error}</div>
  if (!data?.length) return <div className="empty">No open positions</div>

  const q = search.toLowerCase()
  const filtered = data.filter(r =>
    (r.stock_code || '').toLowerCase().includes(q) ||
    (r.strategy   || '').toLowerCase().includes(q) ||
    (r.market     || '').toLowerCase().includes(q)
  )
  const sorted = [...filtered].sort((a, b) => {
    const va = a[sortKey] ?? '', vb = b[sortKey] ?? ''
    return sortAsc ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1)
  })

  const col = (key, label) => (
    <th className={sortKey === key ? 'sort-active' : ''}
        onClick={() => { setSortKey(key); setSortAsc(sortKey === key ? !sortAsc : true) }}>
      {label}{sortKey === key ? (sortAsc ? ' ▲' : ' ▼') : ''}
    </th>
  )

  const fmt2 = v => v != null
    ? `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '—'

  return (
    <div className="table-card">
      <div className="table-toolbar">
        <div className="table-toolbar-left">
          <input className="search-input"
                 placeholder="Search stock, strategy…"
                 value={search} onChange={e => setSearch(e.target.value)} />
          <span className="table-count">{filtered.length} / {data.length} positions</span>
        </div>
        {lastUpdated && <span className="last-updated">Updated {lastUpdated}</span>}
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {col('stock_code',            'Stock')}
              {col('strategy',              'Strategy')}
              {col('market',                'Mkt')}
              {col('exp_date',              'Expiry')}
              {col('dte',                   'DTE')}
              {col('num_contracts',         'Qty')}
              {col('net_credit_per_spread', 'Credit/sh')}
              {col('tp_value',              'TP')}
              {col('cl_value',              'CL')}
              {col('days_open',             'Age')}
              <th>Source</th>
              {col('thesis_score',          'Thesis')}
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => {
              const dte   = r.dte
              const score = r.thesis_score
              const scoreColor = score == null ? 'var(--text3)'
                : score >=  0.10 ? 'var(--green)'
                : score <= -0.10 ? 'var(--red)'
                : 'var(--amber)'
              const checkedDate = r.thesis_last_checked
                ? (() => {
                    const d = new Date(r.thesis_last_checked)
                    return d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' })
                  })()
                : null
              return (
                <tr key={i}>
                  <td><span className="stock-code">{r.stock_code}</span></td>
                  <td style={{ color: 'var(--text2)' }}>{r.strategy}</td>
                  <td>
                    <span className="badge" style={{
                      background: r.market === 'HK' ? 'rgba(245,158,11,0.1)' : 'rgba(59,130,246,0.1)',
                      color:      r.market === 'HK' ? 'var(--amber)' : 'var(--accent)',
                      border:     'none', fontSize: 10
                    }}>
                      {r.market}
                    </span>
                  </td>
                  <td style={{ color: 'var(--text2)' }}>
                    {r.exp_date ? String(r.exp_date).slice(0, 10) : '—'}
                  </td>
                  <td className={dteCls(dte)}>
                    {dte != null ? `${dte}d` : '—'}
                  </td>
                  <td>{r.num_contracts ?? '—'}</td>
                  <td style={{ fontFamily: 'monospace', fontSize: 11 }}>
                    {r.net_credit_per_spread != null
                      ? (r.net_credit_per_spread >= 0 ? '+' : '') + r.net_credit_per_spread.toFixed(4)
                      : '—'}
                  </td>
                  <td className="pnl-pos">{r.tp_value != null ? fmt2(r.tp_value) : '—'}</td>
                  <td className="pnl-neg">{r.cl_value != null ? fmt2(r.cl_value) : '—'}</td>
                  <td style={{ color: 'var(--text3)' }}>{r.days_open != null ? `${r.days_open}d` : '—'}</td>
                  <td>{sourceBadge(r.scan_source, r.wheel_type)}</td>
                  <td title={score == null ? 'Not yet checked' : score >= 0.10 ? 'Thesis intact' : score <= -0.10 ? 'Thesis at risk' : 'Thesis neutral'}>
                    {score != null ? (
                      <div style={{ lineHeight: 1.3 }}>
                        <div style={{ fontFamily: 'monospace', fontSize: 11, fontWeight: 700, color: scoreColor }}>
                          {score >= 0 ? '+' : ''}{score.toFixed(3)}
                        </div>
                        {checkedDate && (
                          <div style={{ fontSize: 9, color: 'var(--text3)' }}>{checkedDate}</div>
                        )}
                      </div>
                    ) : '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>
    </div>
  )
}
