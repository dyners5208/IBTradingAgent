import { useRef, useEffect, useState } from 'react'
import { useAutoRefresh } from '../hooks/useAutoRefresh'

function classifyLine(line) {
  const l = line.toLowerCase()
  if (l.includes('error') || l.includes('exception') || l.includes('failed') || l.includes('traceback'))
    return 'log-err'
  if (l.includes('warning') || l.includes('warn') || l.includes('skip') || l.includes('timeout'))
    return 'log-warn'
  if (l.includes('filled') || l.includes('placed') || l.includes('take_profit') || l.includes('✓') || l.includes('ok'))
    return 'log-ok'
  if (line.startsWith('===') || line.startsWith('---') || line.includes('Session started'))
    return 'log-sep'
  if (line.match(/^\[\d{2}:\d{2}:\d{2}\]/))
    return 'log-ts'
  return ''
}

function renderLine(line, i) {
  // Colour the [HH:MM:SS] timestamp differently from the rest of the line
  const tsMatch = line.match(/^(\[\d{2}:\d{2}:\d{2}\])\s?(.*)$/)
  const cls = classifyLine(line)

  if (tsMatch) {
    return (
      <span key={i} className="log-line">
        <span style={{ color: 'var(--text3)', userSelect: 'none' }}>{tsMatch[1]} </span>
        <span className={cls}>{tsMatch[2]}</span>
        {'\n'}
      </span>
    )
  }
  return (
    <span key={i} className={`log-line ${cls}`}>{line}{'\n'}</span>
  )
}

export default function SessionLog() {
  const [lines, setLines] = useState(300)
  const { data, loading, error, lastUpdated, refresh } = useAutoRefresh(
    `/api/log?lines=${lines}`, 30000
  )
  const bodyRef = useRef(null)
  const [autoScroll, setAutoScroll] = useState(true)

  // Auto-scroll to bottom when new data arrives
  useEffect(() => {
    if (autoScroll && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight
    }
  }, [data, autoScroll])

  const handleScroll = () => {
    const el = bodyRef.current
    if (!el) return
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40
    setAutoScroll(atBottom)
  }

  if (loading) return <div className="loading">Loading session log…</div>
  if (error)   return <div className="error">Error: {error}</div>

  const logLines = data?.lines ?? []
  const hasLog   = logLines.length > 0

  return (
    <div className="log-card">
      <div className="log-toolbar">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <span className="log-filename">
            {data?.file ?? 'No log file found'}
          </span>
          {data?.total != null && (
            <span className="log-meta">{data.total} lines total</span>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--text3)', cursor: 'pointer' }}>
            <input type="checkbox" checked={autoScroll}
                   onChange={e => setAutoScroll(e.target.checked)}
                   style={{ accentColor: 'var(--accent)' }} />
            Auto-scroll
          </label>

          <select
            value={lines}
            onChange={e => setLines(Number(e.target.value))}
            style={{
              background: 'var(--surface2)', border: '1px solid var(--border)',
              color: 'var(--text2)', borderRadius: 4, padding: '4px 8px',
              fontSize: 11, cursor: 'pointer', outline: 'none',
            }}
          >
            <option value={100}>Last 100</option>
            <option value={300}>Last 300</option>
            <option value={500}>Last 500</option>
            <option value={1000}>Last 1000</option>
          </select>

          {lastUpdated && (
            <span className="log-meta">Updated {lastUpdated}</span>
          )}
          <button className="refresh-btn" onClick={refresh}>↻ Refresh</button>
        </div>
      </div>

      <div className="log-body" ref={bodyRef} onScroll={handleScroll}>
        {!hasLog
          ? <span style={{ color: 'var(--text3)' }}>
              No log entries yet. Start the agent to see output here.
            </span>
          : logLines.map((line, i) => renderLine(line, i))
        }
      </div>
    </div>
  )
}
