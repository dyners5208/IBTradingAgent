import { useState, useMemo } from 'react'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

const CATEGORIES = ['All', 'US Spreads', 'Russell', 'US Politician', 'Wheel', 'HK Stocks']

function ReasonBadge({ reason }) {
  const cfg = {
    take_profit:        { cls: 'badge-success', label: 'TP Hit' },
    cut_loss:           { cls: 'badge-loss',    label: 'Cut Loss' },
    theta_exit:         { cls: 'badge-info',    label: 'Theta' },
    thesis_exit:        { cls: 'badge-warning', label: 'Thesis' },
    roll:               { cls: 'badge-info',    label: 'Rolled' },
    rolled:             { cls: 'badge-info',    label: 'Rolled' },
    assigned:           { cls: 'badge-warning', label: 'Assigned' },
    expired:            { cls: 'badge-info',    label: 'Expired' },
    manually_closed:    { cls: 'badge-info',    label: 'Manual' },
    cancelled:          { cls: 'badge-closed',  label: 'Cancelled' },
    roll_declined:      { cls: 'badge-warning', label: 'Roll Dec.' },
  }[reason] || { cls: 'badge-info', label: reason || '—' }
  return <span className={`badge ${cfg.cls}`}>{cfg.label}</span>
}

function QualityBadge({ pnl, avgPnl }) {
  if (pnl == null) return null
  if (pnl < 0)
    return <span className="badge badge-loss" style={{ fontSize: 9, marginLeft: 4 }}>Loss</span>
  if (avgPnl != null && pnl >= avgPnl)
    return <span className="badge badge-success" style={{ fontSize: 9, marginLeft: 4 }}>↑ Avg</span>
  return <span className="badge badge-warning" style={{ fontSize: 9, marginLeft: 4 }}>↓ Avg</span>
}

export default function ClosedTrades() {
  const { data: rawData, loading, error } = useAutoRefresh('/api/trades/closed')
  const [search,      setSearch]      = useState('')
  const [sortKey,     setSortKey]     = useState('closed_at')
  const [sortAsc,     setSortAsc]     = useState(false)
  const [catFilter,   setCatFilter]   = useState('All')

  const data = useMemo(
    () => (rawData || []).filter(r => r.close_reason !== 'cancelled'),
    [rawData]
  )

  // Per-category average P&L for quality badge
  const catAvgPnl = useMemo(() => {
    const map = {}
    for (const cat of CATEGORIES.slice(1)) {
      const rows = data.filter(r => r.category === cat && r.close_pnl != null)
      if (rows.length) map[cat] = rows.reduce((s, r) => s + r.close_pnl, 0) / rows.length
    }
    return map
  }, [data])

  if (loading) return <div className="loading">Loading trades…</div>
  if (error)   return <div className="error">Error: {error}</div>
  if (!data.length) return <div className="empty">No closed trades yet</div>

  const q = search.toLowerCase()
  const filtered = data.filter(r => {
    const matchCat = catFilter === 'All' || r.category === catFilter
    const matchQ   = !q || [r.stock_code, r.strategy, r.close_reason, r.category]
                          .some(f => (f || '').toLowerCase().includes(q))
    return matchCat && matchQ
  })

  const sorted = [...filtered].sort((a, b) => {
    const va = a[sortKey] ?? '', vb = b[sortKey] ?? ''
    return sortAsc ? (va > vb ? 1 : -1) : (va < vb ? 1 : -1)
  })

  const col = (key, label, align = 'left') => (
    <th className={sortKey === key ? 'sort-active' : ''}
        style={{ textAlign: align }}
        onClick={() => { setSortKey(key); setSortAsc(sortKey === key ? !sortAsc : true) }}>
      {label}{sortKey === key ? (sortAsc ? ' ▲' : ' ▼') : ''}
    </th>
  )

  const fmt2 = v => v != null
    ? `$${Number(v).toLocaleString('en-US', { minimumFractionDigits: 2 })}` : '—'

  const pnlTotal = filtered.reduce((acc, r) => acc + (r.close_pnl || 0), 0)

  return (
    <div className="table-card">
      <div className="table-toolbar">
        <div className="table-toolbar-left" style={{ gap: 8, flexWrap: 'wrap' }}>
          <input className="search-input"
                 placeholder="Search stock, strategy, reason…"
                 value={search} onChange={e => setSearch(e.target.value)} />
          <select
            value={catFilter}
            onChange={e => setCatFilter(e.target.value)}
            style={{
              background: 'var(--surface2)', border: '1px solid var(--border)',
              borderRadius: 6, color: 'var(--text2)', fontSize: 12,
              padding: '5px 10px', cursor: 'pointer',
            }}
          >
            {CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
          <span className="table-count">{filtered.length} / {data.length} trades</span>
        </div>
        <span className={`table-count ${pnlTotal >= 0 ? 'pnl-pos' : 'pnl-neg'}`}>
          Total: {fmt2(pnlTotal)}
        </span>
      </div>

      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              {col('stock_code',    'Stock')}
              {col('strategy',      'Strategy')}
              {col('category',      'Category')}
              {col('market',        'Mkt')}
              {col('close_reason',  'Reason')}
              {col('duration_days', 'Dur', 'right')}
              {col('income_usd',    'Income', 'right')}
              {col('close_pnl',     'P&L', 'right')}
              {col('roi_pct',       'ROI%', 'right')}
              {col('opened_at',     'Opened')}
              {col('closed_at',     'Closed')}
              {col('exp_date',      'Expiry')}
            </tr>
          </thead>
          <tbody>
            {sorted.map((r, i) => {
              const pnl    = r.close_pnl ?? 0
              const avgPnl = catAvgPnl[r.category]
              return (
                <tr key={i}>
                  <td><span className="stock-code">{r.stock_code}</span></td>
                  <td style={{ color: 'var(--text2)', fontSize: 11 }}>{r.strategy}</td>
                  <td style={{ color: 'var(--text3)', fontSize: 11 }}>{r.category || '—'}</td>
                  <td style={{ color: 'var(--text3)', fontSize: 11 }}>{r.market}</td>
                  <td><ReasonBadge reason={r.close_reason} /></td>
                  <td style={{ textAlign: 'right', color: 'var(--text3)', fontSize: 11 }}>
                    {r.duration_days != null ? `${r.duration_days}d` : '—'}
                  </td>
                  <td style={{ textAlign: 'right', color: 'var(--text2)', fontFamily: 'monospace', fontSize: 11 }}>
                    {r.income_usd != null ? fmt2(r.income_usd) : '—'}
                  </td>
                  <td style={{ textAlign: 'right' }}
                      className={pnl > 0 ? 'pnl-pos' : pnl < 0 ? 'pnl-neg' : 'pnl-zero'}>
                    {fmt2(r.close_pnl)}
                    <QualityBadge pnl={r.close_pnl} avgPnl={avgPnl} />
                  </td>
                  <td style={{ textAlign: 'right' }}
                      className={r.roi_pct == null ? '' : r.roi_pct >= 0 ? 'pnl-pos' : 'pnl-neg'}>
                    {r.roi_pct != null ? `${r.roi_pct}%` : '—'}
                  </td>
                  <td style={{ color: 'var(--text3)', fontSize: 11 }}>
                    {r.opened_at ? String(r.opened_at).slice(0, 10) : '—'}
                  </td>
                  <td style={{ color: 'var(--text2)', fontSize: 11 }}>
                    {r.closed_at ? String(r.closed_at).slice(0, 10) : '—'}
                  </td>
                  <td style={{ color: 'var(--text3)', fontSize: 11 }}>
                    {r.exp_date ? String(r.exp_date).slice(0, 10) : '—'}
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
