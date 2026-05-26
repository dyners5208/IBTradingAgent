import { useAutoRefresh } from '../hooks/useAutoRefresh'

function LevelBadge({ level }) {
  const cfg = {
    CRITICAL: 'badge-critical',
    WARNING:  'badge-warning',
    INFO:     'badge-info',
  }[level] || 'badge-info'
  return <span className={`badge ${cfg}`}>{level || 'INFO'}</span>
}

function fmtTs(ts) {
  if (!ts) return '—'
  return String(ts).slice(0, 19).replace('T', ' ')
}

export default function Alerts() {
  const { data, loading, error, lastUpdated, refresh } = useAutoRefresh('/api/alerts')

  if (loading) return <div className="loading">Loading alerts…</div>
  if (error)   return <div className="error">Error: {error}</div>
  if (!data?.length) return <div className="empty">No alerts recorded</div>

  const critCount = data.filter(a => a.level === 'CRITICAL').length
  const warnCount = data.filter(a => a.level === 'WARNING').length

  return (
    <>
      <div className="cards-row" style={{ marginBottom: 16 }}>
        <div className="card red-top">
          <div className="label">Critical</div>
          <div className="value red">{critCount}</div>
        </div>
        <div className="card amber-top">
          <div className="label">Warning</div>
          <div className="value amber">{warnCount}</div>
        </div>
        <div className="card">
          <div className="label">Total (last 200)</div>
          <div className="value">{data.length}</div>
        </div>
      </div>

      <div className="table-card">
        <div className="table-toolbar">
          <span style={{ fontSize: 11, color: 'var(--text3)' }}>
            Showing newest {data.length} alerts
          </span>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {lastUpdated && <span className="last-updated">Updated {lastUpdated}</span>}
            <button className="refresh-btn" onClick={refresh}>↻ Refresh</button>
          </div>
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th style={{ width: 160 }}>Time</th>
                <th style={{ width: 90 }}>Level</th>
                <th>Message</th>
                <th style={{ width: 120 }}>Context</th>
              </tr>
            </thead>
            <tbody>
              {data.map((a, i) => (
                <tr key={i} style={a.level === 'CRITICAL' ? { background: 'rgba(239,68,68,0.04)' } : {}}>
                  <td style={{ fontFamily: 'monospace', fontSize: 11, color: 'var(--text3)' }}>
                    {fmtTs(a.timestamp)}
                  </td>
                  <td><LevelBadge level={a.level} /></td>
                  <td style={{ fontSize: 12, color: 'var(--text2)', maxWidth: 500, whiteSpace: 'normal' }}>
                    {a.message}
                  </td>
                  <td style={{ fontSize: 11, color: 'var(--text3)', fontFamily: 'monospace' }}>
                    {a.context ? JSON.stringify(a.context).slice(0, 60) : '—'}
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
