import { useState, useEffect, useCallback } from 'react'

const API = import.meta.env.DEV ? 'http://localhost:8050' : ''

const CONVICTION_LABELS = ['', 'Speculative', 'Exploratory', 'Moderate', 'Strong', 'High']
const CONVICTION_COLORS = ['', '#6b7280', '#3b82f6', '#f59e0b', '#10b981', '#8b5cf6']

function convColor(v) {
  return CONVICTION_COLORS[Math.max(1, Math.min(5, Number(v) || 1))]
}
function convLabel(v) {
  return CONVICTION_LABELS[Math.max(1, Math.min(5, Number(v) || 1))]
}

function Stars({ value, size = 11 }) {
  const n = Math.max(1, Math.min(5, Number(value) || 1))
  return (
    <span style={{ color: convColor(n), fontSize: size, letterSpacing: 1, lineHeight: 1 }}>
      {'★'.repeat(n)}{'☆'.repeat(5 - n)}
    </span>
  )
}

function ConvBadge({ value }) {
  const n = Math.max(1, Math.min(5, Number(value) || 1))
  const c = convColor(n)
  return (
    <span style={{ background: `${c}22`, color: c, border: `1px solid ${c}55`,
                   borderRadius: 20, padding: '3px 10px', fontSize: 11, fontWeight: 700 }}>
      {'★'.repeat(n)}{'☆'.repeat(5 - n)}&nbsp;&nbsp;{convLabel(n)}
    </span>
  )
}

function Chip({ label, color, bg, border }) {
  return (
    <span style={{ fontSize: 11, color: color || 'var(--text3)', background: bg || 'var(--surface3)',
                   border: `1px solid ${border || 'var(--border)'}`,
                   borderRadius: 20, padding: '3px 10px', whiteSpace: 'nowrap' }}>
      {label}
    </span>
  )
}

function MktPill({ code }) {
  const isHK = String(code).startsWith('HK.')
  return (
    <span style={{
      fontSize: 9, padding: '1px 5px', borderRadius: 4, fontWeight: 700,
      background: isHK ? 'rgba(16,185,129,0.15)' : 'rgba(59,130,246,0.15)',
      color: isHK ? 'var(--green)' : 'var(--accent)',
      border: `1px solid ${isHK ? 'rgba(16,185,129,0.3)' : 'rgba(59,130,246,0.3)'}`,
    }}>
      {isHK ? 'HK' : 'US'}
    </span>
  )
}

/* Extract the first line of a moat string as the moat TYPE label */
function moatType(moatStr) {
  if (!moatStr) return null
  return moatStr.split('\n')[0].trim() || null
}

/* Return the rest of the moat string (evidence lines) */
function moatEvidence(moatStr) {
  if (!moatStr) return null
  const lines = moatStr.split('\n')
  return lines.slice(1).join('\n').trim() || null
}

/* Ratio delta colour: green = cheaper than sector, amber = 0–50% premium, red = >50% premium */
function ratioColor(stock, sector) {
  if (stock == null || sector == null || sector === 0) return 'var(--text)'
  const pct = ((stock - sector) / Math.abs(sector)) * 100
  if (pct < 0) return 'var(--green)'
  if (pct <= 50) return 'var(--amber)'
  return 'var(--red)'
}

function ratioDelta(stock, sector) {
  if (stock == null || sector == null || sector === 0) return null
  const pct = ((stock - sector) / Math.abs(sector)) * 100
  return (pct >= 0 ? '+' : '') + pct.toFixed(0) + '%'
}

function fmt(n, decimals = 1) {
  if (n == null) return '—'
  return Number(n).toFixed(decimals)
}

/* ── Left sidebar gem card ─────────────────────────────────────────────────── */
function GemCard({ gem, selected, onClick }) {
  const scoreNull = gem.last_score == null
  const scoreColor = scoreNull ? 'var(--text3)'
    : Math.abs(gem.last_score) >= 0.15 ? 'var(--green)' : 'var(--amber)'
  const hasScore = !scoreNull
  const meta = gem.metadata || {}
  const mt = moatType(meta.moat)

  return (
    <div onClick={onClick} style={{
      padding: '11px 14px',
      borderBottom: '1px solid var(--border)',
      cursor: 'pointer',
      borderLeft: `3px solid ${selected ? 'var(--accent)' : 'transparent'}`,
      background: selected ? 'rgba(59,130,246,0.08)' : 'transparent',
      transition: 'background 0.1s',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 3 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontWeight: 700, fontSize: 12, fontFamily: 'monospace',
                         color: selected ? 'var(--accent)' : 'var(--text)' }}>
            {gem.stock_code}
          </span>
          <MktPill code={gem.stock_code} />
        </div>
        <span style={{ fontSize: 10, fontFamily: 'monospace', fontWeight: 600, color: scoreColor }}>
          {hasScore ? gem.last_score.toFixed(3) : '—'}
        </span>
      </div>
      <div style={{ fontSize: 11, color: 'var(--text2)', marginBottom: 4,
                    overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    maxWidth: 200 }}>
        {gem.name || '—'}
      </div>
      {mt && (
        <div style={{ marginBottom: 4 }}>
          <span style={{ fontSize: 9, background: 'rgba(245,158,11,0.12)',
                         color: 'var(--amber)', border: '1px solid rgba(245,158,11,0.3)',
                         borderRadius: 20, padding: '2px 8px', whiteSpace: 'nowrap' }}>
            {mt}
          </span>
        </div>
      )}
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Stars value={gem.conviction} size={10} />
        <span style={{ fontSize: 9, color: 'var(--text3)',
                       overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                       maxWidth: 100, textAlign: 'right' }}>
          {gem.sector || ''}
        </span>
      </div>
    </div>
  )
}

/* ── Right panel: empty state ─────────────────────────────────────────────── */
function EmptyState() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center',
                  justifyContent: 'center', height: '100%', gap: 12 }}>
      <span style={{ fontSize: 40, lineHeight: 1 }}>💎</span>
      <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text2)' }}>
        Select a gem to view its research
      </div>
      <div style={{ fontSize: 12, color: 'var(--text3)' }}>
        Or research a new theme using the bar above
      </div>
    </div>
  )
}

/* ── Right panel: searching ───────────────────────────────────────────────── */
function SearchingState() {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center',
                  justifyContent: 'center', height: '100%', gap: 16 }}>
      <span style={{ fontSize: 36, animation: 'spin 1.2s linear infinite', display: 'block' }}>⟳</span>
      <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text)' }}>Researching…</div>
      <div style={{ fontSize: 12, color: 'var(--text3)', textAlign: 'center', lineHeight: 1.6 }}>
        Claude is searching the web and analysing candidates.<br />
        This typically takes 30–60 seconds.
      </div>
    </div>
  )
}

/* ── Moat section ─────────────────────────────────────────────────────────── */
function MoatSection({ moat }) {
  if (!moat) return null
  const type = moatType(moat)
  const evidence = moatEvidence(moat)
  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                    letterSpacing: '0.7px', color: 'var(--text3)', marginBottom: 10 }}>
        Competitive Moat
      </div>
      <div style={{
        background: 'rgba(245,158,11,0.06)',
        border: '1px solid rgba(245,158,11,0.25)',
        borderLeft: '3px solid var(--amber)',
        borderRadius: '0 8px 8px 0',
        padding: '14px 18px',
      }}>
        {type && (
          <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--amber)', marginBottom: evidence ? 8 : 0 }}>
            {type}
          </div>
        )}
        {evidence && (
          <div style={{ fontSize: 12, color: 'var(--text2)', lineHeight: 1.75,
                        whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
            {evidence}
          </div>
        )}
      </div>
    </div>
  )
}

/* ── Valuation vs Sector table ────────────────────────────────────────────── */
function ValuationSection({ meta }) {
  const rows = [
    { label: 'P/E', stock: meta.pe_ratio, sector: meta.pe_sector_avg },
    { label: 'P/B', stock: meta.pb_ratio, sector: meta.pb_sector_avg },
    { label: 'P/S', stock: meta.ps_ratio, sector: meta.ps_sector_avg },
  ]
  const hasAny = rows.some(r => r.stock != null || r.sector != null)
  if (!hasAny) return null

  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                    letterSpacing: '0.7px', color: 'var(--text3)', marginBottom: 10 }}>
        Valuation vs Sector
      </div>
      <div style={{
        background: 'var(--surface2)', border: '1px solid var(--border)',
        borderRadius: 8, overflow: 'hidden',
      }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
          <thead>
            <tr style={{ background: 'var(--surface3)' }}>
              {['Metric', 'Stock', 'Sector Avg', 'vs Sector'].map(h => (
                <th key={h} style={{ padding: '8px 14px', textAlign: h === 'Metric' ? 'left' : 'right',
                                     fontSize: 9, fontWeight: 700, textTransform: 'uppercase',
                                     letterSpacing: '0.5px', color: 'var(--text3)',
                                     borderBottom: '1px solid var(--border)' }}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map(({ label, stock, sector }) => {
              const color = ratioColor(stock, sector)
              const delta = ratioDelta(stock, sector)
              return (
                <tr key={label} style={{ borderBottom: '1px solid var(--border)' }}>
                  <td style={{ padding: '9px 14px', fontWeight: 700, color: 'var(--text)' }}>{label}</td>
                  <td style={{ padding: '9px 14px', textAlign: 'right', fontFamily: 'monospace',
                               fontWeight: 600, color: color }}>
                    {stock != null ? fmt(stock) : '—'}
                  </td>
                  <td style={{ padding: '9px 14px', textAlign: 'right', fontFamily: 'monospace',
                               color: 'var(--text2)' }}>
                    {sector != null ? fmt(sector) : '—'}
                  </td>
                  <td style={{ padding: '9px 14px', textAlign: 'right', fontFamily: 'monospace',
                               fontWeight: 700, color: color }}>
                    {delta || '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <div style={{ padding: '6px 14px', fontSize: 10, color: 'var(--text3)',
                      borderTop: '1px solid var(--border)' }}>
          Green = cheaper than sector · Amber = 0–50% premium · Red = &gt;50% premium
        </div>
      </div>
    </div>
  )
}

/* ── Financial Health section ─────────────────────────────────────────────── */
function FinancialSection({ meta }) {
  const metrics = [
    { label: 'Revenue Growth', value: meta.revenue_growth_yoy_pct != null
        ? `${meta.revenue_growth_yoy_pct >= 0 ? '+' : ''}${fmt(meta.revenue_growth_yoy_pct)}% YoY`
        : null,
      color: meta.revenue_growth_yoy_pct > 0 ? 'var(--green)' : 'var(--red)' },
    { label: 'Gross Margin', value: meta.gross_margin_pct != null
        ? `${fmt(meta.gross_margin_pct)}%` : null,
      color: 'var(--text)' },
    { label: 'Debt / Equity', value: meta.debt_to_equity != null
        ? fmt(meta.debt_to_equity, 2) : null,
      color: 'var(--text)' },
    { label: 'D/E Sector Avg', value: meta.debt_to_equity_sector_avg != null
        ? fmt(meta.debt_to_equity_sector_avg, 2) : null,
      color: 'var(--text2)' },
  ]
  const hasAny = metrics.some(m => m.value != null) || meta.metrics_note
  if (!hasAny) return null

  return (
    <div style={{ marginBottom: 22 }}>
      <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                    letterSpacing: '0.7px', color: 'var(--text3)', marginBottom: 10 }}>
        Financial Health
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 8 }}>
        {metrics.filter(m => m.value != null).map(({ label, value, color }) => (
          <div key={label} style={{
            background: 'var(--surface2)', border: '1px solid var(--border)',
            borderRadius: 8, padding: '11px 14px',
          }}>
            <div style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase',
                          letterSpacing: '0.5px', color: 'var(--text3)', marginBottom: 5 }}>
              {label}
            </div>
            <div style={{ fontSize: 14, fontWeight: 700, color, fontFamily: 'monospace' }}>
              {value}
            </div>
          </div>
        ))}
      </div>
      {meta.metrics_note && (
        <div style={{ marginTop: 8, fontSize: 11, color: 'var(--text3)', lineHeight: 1.6,
                      fontStyle: 'italic' }}>
          {meta.metrics_note}
        </div>
      )}
    </div>
  )
}

/* ── Right panel: gem detail ──────────────────────────────────────────────── */
function GemDetail({ gem, onRemove }) {
  const scoreNull = gem.last_score == null
  const scoreColor = scoreNull ? 'var(--text3)'
    : Math.abs(gem.last_score) >= 0.15 ? 'var(--green)' : 'var(--amber)'
  const aboveThreshold = !scoreNull && Math.abs(gem.last_score) >= 0.15
  const meta = gem.metadata || {}

  return (
    <div style={{ padding: '28px 32px', overflowY: 'auto', height: '100%' }}>

      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start',
                    justifyContent: 'space-between', gap: 16, marginBottom: 20 }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', marginBottom: 8 }}>
            <span style={{ fontSize: 24, fontWeight: 800, fontFamily: 'monospace',
                           color: 'var(--text)', letterSpacing: '-0.5px' }}>
              {gem.stock_code}
            </span>
            <MktPill code={gem.stock_code} />
            <ConvBadge value={gem.conviction} />
          </div>
          <div style={{ fontSize: 15, color: 'var(--text2)', fontWeight: 500, marginBottom: 12 }}>
            {gem.name}
          </div>
          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            {gem.sector && <Chip label={gem.sector} />}
            {gem.macro_theme && (
              <Chip label={gem.macro_theme}
                color="var(--accent)" bg="var(--accent-dim)"
                border="rgba(59,130,246,0.3)" />
            )}
          </div>
        </div>
        <button onClick={() => onRemove(gem)} style={{
          flexShrink: 0, background: 'transparent',
          border: '1px solid rgba(239,68,68,0.4)', borderRadius: 6,
          color: 'var(--red)', fontSize: 11, fontWeight: 600,
          padding: '6px 14px', cursor: 'pointer',
          transition: 'background 0.15s',
        }}
          onMouseEnter={e => e.currentTarget.style.background = 'var(--red-dim)'}
          onMouseLeave={e => e.currentTarget.style.background = 'transparent'}
        >
          Remove Gem
        </button>
      </div>

      {/* Score banner — only if scored */}
      {!scoreNull && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 20, flexWrap: 'wrap',
          background: aboveThreshold ? 'rgba(16,185,129,0.07)' : 'rgba(245,158,11,0.07)',
          border: `1px solid ${aboveThreshold ? 'rgba(16,185,129,0.25)' : 'rgba(245,158,11,0.25)'}`,
          borderRadius: 8, padding: '14px 18px', marginBottom: 22,
        }}>
          <div>
            <div style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase',
                          letterSpacing: '0.5px', color: 'var(--text3)', marginBottom: 3 }}>
              Technical Score
            </div>
            <div style={{ fontSize: 26, fontWeight: 800, fontFamily: 'monospace', color: scoreColor }}>
              {gem.last_score.toFixed(4)}
            </div>
          </div>
          <div style={{ flex: 1, minWidth: 160 }}>
            <div style={{ fontSize: 12, color: 'var(--text2)', lineHeight: 1.6 }}>
              {aboveThreshold
                ? 'Score meets the entry threshold (≥ 0.15). The agent will enter a position when a gem slot is available.'
                : 'Score is below entry threshold (0.15). The agent is watching but will not enter yet.'}
            </div>
          </div>
          <div style={{ textAlign: 'right' }}>
            <div style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase',
                          letterSpacing: '0.5px', color: 'var(--text3)', marginBottom: 3 }}>
              Last Scored
            </div>
            <div style={{ fontSize: 12, color: 'var(--text2)' }}>
              {gem.last_scored || '—'}
            </div>
          </div>
        </div>
      )}

      {/* Thesis */}
      {gem.thesis && (
        <div style={{ marginBottom: 22 }}>
          <div style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                        letterSpacing: '0.7px', color: 'var(--text3)', marginBottom: 10 }}>
            Investment Thesis
          </div>
          <div style={{
            fontSize: 13, color: 'var(--text)', lineHeight: 1.8,
            background: 'var(--surface2)',
            border: '1px solid var(--border)',
            borderLeft: '3px solid var(--accent)',
            borderRadius: '0 8px 8px 0',
            padding: '16px 20px',
            whiteSpace: 'pre-wrap',
            wordBreak: 'break-word',
          }}>
            {gem.thesis}
          </div>
        </div>
      )}

      {/* Competitive Moat */}
      <MoatSection moat={meta.moat} />

      {/* Valuation vs Sector */}
      <ValuationSection meta={meta} />

      {/* Financial Health */}
      <FinancialSection meta={meta} />

      {/* Metadata grid */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
                    gap: 10 }}>
        {[
          { label: 'Added', value: gem.added_at?.slice(0, 10) || '—', color: 'var(--text)' },
          { label: 'Last Scored', value: gem.last_scored || 'Never', color: 'var(--text)' },
          { label: 'Technical Score',
            value: scoreNull ? 'Not yet scored' : gem.last_score.toFixed(4),
            color: scoreColor },
          { label: 'Conviction',
            value: `${gem.conviction}/5 — ${convLabel(gem.conviction)}`,
            color: convColor(gem.conviction) },
        ].map(({ label, value, color }) => (
          <div key={label} style={{
            background: 'var(--surface2)', border: '1px solid var(--border)',
            borderRadius: 8, padding: '12px 14px',
          }}>
            <div style={{ fontSize: 9, fontWeight: 700, textTransform: 'uppercase',
                          letterSpacing: '0.5px', color: 'var(--text3)', marginBottom: 5 }}>
              {label}
            </div>
            <div style={{ fontSize: 13, fontWeight: 600, color }}>
              {value}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

/* ── Right panel: research candidates ────────────────────────────────────── */
function CandidatesPanel({ candidates, selected, onToggle, onAdd, onClear, addMsg, err }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Sticky header inside panel */}
      <div style={{
        padding: '18px 28px 14px', borderBottom: '1px solid var(--border)',
        background: 'var(--surface)', flexShrink: 0,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12,
      }}>
        <div>
          <div style={{ fontSize: 13, fontWeight: 700, color: 'var(--text)', marginBottom: 2 }}>
            Research Results
          </div>
          <div style={{ fontSize: 11, color: 'var(--text3)' }}>
            {candidates.length} candidate{candidates.length !== 1 ? 's' : ''} found — tick the ones you want to add
          </div>
        </div>
        <div style={{ display: 'flex', gap: 8, flexShrink: 0 }}>
          <button onClick={onClear} style={{
            background: 'var(--surface2)', border: '1px solid var(--border)', borderRadius: 6,
            color: 'var(--text2)', fontSize: 11, fontWeight: 600, padding: '6px 14px', cursor: 'pointer',
          }}>
            Clear
          </button>
          <button onClick={onAdd} disabled={selected.size === 0} style={{
            background: selected.size === 0 ? 'var(--surface3)' : 'var(--green)',
            border: 'none', borderRadius: 6,
            color: selected.size === 0 ? 'var(--text3)' : '#fff',
            fontSize: 11, fontWeight: 700, padding: '6px 16px',
            cursor: selected.size === 0 ? 'not-allowed' : 'pointer',
            transition: 'background 0.15s',
          }}>
            Add Selected ({selected.size})
          </button>
        </div>
      </div>

      {(addMsg || err) && (
        <div style={{
          margin: '12px 28px 0',
          background: err ? 'var(--red-dim)' : 'var(--green-dim)',
          border: `1px solid ${err ? 'rgba(239,68,68,0.3)' : 'rgba(16,185,129,0.3)'}`,
          borderRadius: 6, padding: '10px 14px', fontSize: 12,
          color: err ? 'var(--red)' : 'var(--green)',
        }}>
          {err || addMsg}
        </div>
      )}

      {/* Scrollable candidate list */}
      <div style={{ overflowY: 'auto', flex: 1, padding: '16px 28px 24px' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {candidates.map(c => {
            const isChecked = selected.has(c.stock_code)
            const mt = moatType(c.moat)
            const peColor = ratioColor(c.pe_ratio, c.pe_sector_avg)
            const peLine = c.pe_ratio != null
              ? `P/E ${fmt(c.pe_ratio)} vs sector ${c.pe_sector_avg != null ? fmt(c.pe_sector_avg) : '—'}`
              : 'Pre-profit / P/E N/A'
            const revGrowth = c.revenue_growth_yoy_pct != null
              ? `${c.revenue_growth_yoy_pct >= 0 ? '+' : ''}${fmt(c.revenue_growth_yoy_pct)}% YoY`
              : null
            return (
              <div key={c.stock_code} onClick={() => onToggle(c.stock_code)}
                style={{
                  background: isChecked ? 'rgba(59,130,246,0.07)' : 'var(--surface2)',
                  border: `1px solid ${isChecked ? 'rgba(59,130,246,0.4)' : 'var(--border)'}`,
                  borderRadius: 8, padding: '14px 16px', cursor: 'pointer',
                  transition: 'border-color 0.15s, background 0.15s',
                }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12 }}>
                  <input type="checkbox" checked={isChecked} onChange={() => onToggle(c.stock_code)}
                    onClick={e => e.stopPropagation()}
                    style={{ marginTop: 2, accentColor: 'var(--accent)', cursor: 'pointer',
                             width: 14, height: 14, flexShrink: 0 }} />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {/* Title row */}
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8,
                                  flexWrap: 'wrap', marginBottom: 6 }}>
                      <span style={{ fontWeight: 700, fontSize: 13, fontFamily: 'monospace',
                                     color: 'var(--text)' }}>{c.stock_code}</span>
                      <MktPill code={c.stock_code} />
                      <span style={{ fontSize: 12, color: 'var(--text2)' }}>{c.name}</span>
                      <ConvBadge value={c.conviction} />
                    </div>

                    {/* Sector / theme chips */}
                    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 8 }}>
                      {c.sector && <Chip label={c.sector} />}
                      {c.macro_theme && (
                        <Chip label={c.macro_theme} color="var(--accent)"
                          bg="var(--accent-dim)" border="rgba(59,130,246,0.25)" />
                      )}
                      {mt && (
                        <span style={{ fontSize: 11, background: 'rgba(245,158,11,0.12)',
                                       color: 'var(--amber)', border: '1px solid rgba(245,158,11,0.3)',
                                       borderRadius: 20, padding: '3px 10px', whiteSpace: 'nowrap' }}>
                          {mt}
                        </span>
                      )}
                    </div>

                    {/* Key metrics row */}
                    <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap',
                                  fontSize: 11, color: 'var(--text3)', marginBottom: 8 }}>
                      <span style={{ color: peColor }}>{peLine}</span>
                      {revGrowth && (
                        <span style={{ color: c.revenue_growth_yoy_pct > 0 ? 'var(--green)' : 'var(--red)',
                                       fontWeight: 600 }}>
                          {revGrowth} revenue
                        </span>
                      )}
                      {c.gross_margin_pct != null && (
                        <span>{fmt(c.gross_margin_pct)}% margin</span>
                      )}
                    </div>

                    {/* Thesis */}
                    <div style={{ fontSize: 12, color: 'var(--text2)', lineHeight: 1.65,
                                  wordBreak: 'break-word' }}>
                      {c.thesis}
                    </div>

                    {/* Metrics note */}
                    {c.metrics_note && (
                      <div style={{ marginTop: 6, fontSize: 11, color: 'var(--text3)',
                                    fontStyle: 'italic' }}>
                        {c.metrics_note}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}

/* ── Remove modal ─────────────────────────────────────────────────────────── */
function RemoveModal({ gem, onConfirm, onCancel }) {
  const [reason, setReason] = useState('')
  return (
    <div style={{ position: 'fixed', inset: 0, background: 'rgba(0,0,0,0.7)',
                  display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000 }}>
      <div style={{ background: 'var(--surface)', border: '1px solid var(--border2)',
                    borderRadius: 10, padding: 28, width: 420, maxWidth: '90vw' }}>
        <div style={{ fontSize: 15, fontWeight: 700, marginBottom: 6 }}>Remove gem</div>
        <div style={{ fontSize: 12, color: 'var(--text2)', marginBottom: 18 }}>
          Removing <strong style={{ color: 'var(--text)', fontFamily: 'monospace' }}>
            {gem.stock_code}
          </strong> — {gem.name}
        </div>
        <label style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                        letterSpacing: '0.5px', color: 'var(--text3)', display: 'block', marginBottom: 6 }}>
          Reason (optional)
        </label>
        <input autoFocus value={reason} onChange={e => setReason(e.target.value)}
          onKeyDown={e => { if (e.key === 'Enter') onConfirm(reason); if (e.key === 'Escape') onCancel() }}
          placeholder="e.g. thesis broken — competition entering"
          style={{ width: '100%', background: 'var(--surface2)', border: '1px solid var(--border)',
                   borderRadius: 6, padding: '9px 12px', fontSize: 13, color: 'var(--text)',
                   outline: 'none', marginBottom: 20 }} />
        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
          <button onClick={onCancel} style={{ background: 'var(--surface2)',
            border: '1px solid var(--border)', borderRadius: 6, color: 'var(--text2)',
            fontSize: 12, padding: '8px 18px', cursor: 'pointer' }}>
            Cancel
          </button>
          <button onClick={() => onConfirm(reason)} style={{ background: 'var(--red)',
            border: 'none', borderRadius: 6, color: '#fff',
            fontSize: 12, fontWeight: 700, padding: '8px 18px', cursor: 'pointer' }}>
            Remove
          </button>
        </div>
      </div>
    </div>
  )
}

/* ── Main component ───────────────────────────────────────────────────────── */
export default function Gems() {
  // Research state
  const [theme, setTheme]             = useState('')
  const [researching, setResearching] = useState(false)
  const [candidates, setCandidates]   = useState([])
  const [selected, setSelected]       = useState(new Set())
  const [addMsg, setAddMsg]           = useState('')
  const [researchErr, setResearchErr] = useState('')

  // Gem universe state
  const [gems, setGems]               = useState([])
  const [gemsLoading, setGemsLoading] = useState(true)
  const [selectedGem, setSelectedGem] = useState(null)
  const [removeTarget, setRemoveTarget] = useState(null)

  // Right panel mode: 'empty' | 'searching' | 'candidates' | 'detail'
  const rightMode = researching ? 'searching'
    : candidates.length > 0    ? 'candidates'
    : selectedGem               ? 'detail'
    : 'empty'

  const loadGems = useCallback(() => {
    setGemsLoading(true)
    fetch(`${API}/api/gems`)
      .then(r => r.json())
      .then(d => {
        const list = Array.isArray(d) ? d : []
        setGems(list)
        setSelectedGem(prev => prev ? (list.find(g => g.stock_code === prev.stock_code) ?? null) : null)
      })
      .catch(() => setGems([]))
      .finally(() => setGemsLoading(false))
  }, [])

  useEffect(() => { loadGems() }, [loadGems])

  const handleResearch = async () => {
    if (!theme.trim() || researching) return
    setResearching(true)
    setResearchErr('')
    setCandidates([])
    setSelected(new Set())
    setAddMsg('')
    try {
      const r = await fetch(`${API}/api/gems/research`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ theme: theme.trim() }),
      })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || 'Research failed')
      if (!data.candidates?.length) {
        setResearchErr('No candidates found. Try a more specific theme.')
      } else {
        setCandidates(data.candidates)
      }
    } catch (e) {
      setResearchErr(e.message)
    } finally {
      setResearching(false)
    }
  }

  const handleAddSelected = async () => {
    const toAdd = candidates.filter(c => selected.has(c.stock_code))
    if (!toAdd.length) return
    try {
      const r = await fetch(`${API}/api/gems/add`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ gems: toAdd }),
      })
      const data = await r.json()
      if (!r.ok) throw new Error(data.detail || 'Failed to add')
      setAddMsg(`Added ${(data.added || []).length} gem${(data.added || []).length !== 1 ? 's' : ''}: ${(data.added || []).join(', ')}`)
      setCandidates([])
      setSelected(new Set())
      await loadGems()
      if (data.added?.length) {
        setTimeout(() => {
          setGems(prev => {
            const gem = prev.find(g => g.stock_code === data.added[0])
            if (gem) setSelectedGem(gem)
            return prev
          })
        }, 100)
      }
    } catch (e) {
      setResearchErr(e.message)
    }
  }

  const handleRemove = async (reason) => {
    if (!removeTarget) return
    try {
      await fetch(`${API}/api/gems/remove`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ stock_code: removeTarget.stock_code, reason }),
      })
      setRemoveTarget(null)
      loadGems()
    } catch { setRemoveTarget(null) }
  }

  const toggleCandidate = code => setSelected(prev => {
    const next = new Set(prev)
    next.has(code) ? next.delete(code) : next.add(code)
    return next
  })

  return (
    <div style={{
      margin: '-20px -24px -40px',
      height: 'calc(100vh - 104px)',
      display: 'flex', flexDirection: 'column',
      overflow: 'hidden',
    }}>

      {/* ── Research bar ────────────────────────────────────────────────── */}
      <div style={{
        flexShrink: 0, padding: '10px 20px',
        background: 'var(--surface)',
        borderBottom: '1px solid var(--border)',
        display: 'flex', flexDirection: 'column', gap: 8,
      }}>
        <div style={{ display: 'flex', gap: 10, alignItems: 'center' }}>
          <span style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                         letterSpacing: '0.6px', color: 'var(--text3)', whiteSpace: 'nowrap',
                         flexShrink: 0 }}>
            Research Theme
          </span>
          <input
            value={theme}
            onChange={e => setTheme(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') handleResearch() }}
            disabled={researching}
            placeholder="e.g. space and robotics, GLP-1 ecosystem, AI data centres, nuclear energy…"
            style={{ flex: 1, background: 'var(--surface2)', border: '1px solid var(--border)',
                     borderRadius: 6, padding: '7px 12px', fontSize: 12, color: 'var(--text)',
                     outline: 'none', minWidth: 0 }}
          />
          <button onClick={handleResearch} disabled={researching || !theme.trim()}
            style={{
              flexShrink: 0, fontWeight: 700, fontSize: 12, padding: '7px 20px',
              borderRadius: 6, border: 'none', cursor: researching || !theme.trim() ? 'not-allowed' : 'pointer',
              background: researching || !theme.trim() ? 'var(--surface3)' : 'var(--accent)',
              color: researching || !theme.trim() ? 'var(--text3)' : '#fff',
              transition: 'background 0.15s',
            }}>
            {researching ? 'Researching…' : 'Research'}
          </button>
        </div>

        {(researchErr || addMsg) && (
          <div style={{
            fontSize: 11, padding: '6px 12px', borderRadius: 6,
            background: researchErr ? 'var(--red-dim)' : 'var(--green-dim)',
            border: `1px solid ${researchErr ? 'rgba(239,68,68,0.3)' : 'rgba(16,185,129,0.3)'}`,
            color: researchErr ? 'var(--red)' : 'var(--green)',
          }}>
            {researchErr || addMsg}
            <button onClick={() => { setResearchErr(''); setAddMsg('') }}
              style={{ float: 'right', background: 'none', border: 'none',
                       color: 'inherit', cursor: 'pointer', fontSize: 11, padding: 0 }}>
              ✕
            </button>
          </div>
        )}
      </div>

      {/* ── Main two-panel body ──────────────────────────────────────────── */}
      <div style={{ flex: 1, display: 'flex', minHeight: 0 }}>

        {/* Left sidebar */}
        <div style={{
          width: 260, flexShrink: 0,
          borderRight: '1px solid var(--border)',
          display: 'flex', flexDirection: 'column',
          background: 'var(--surface)',
        }}>
          <div style={{
            padding: '10px 14px',
            borderBottom: '1px solid var(--border)',
            background: 'var(--surface2)',
            display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          }}>
            <span style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase',
                           letterSpacing: '0.6px', color: 'var(--text3)' }}>
              Gem Universe
            </span>
            <span style={{ fontSize: 10, color: 'var(--text3)' }}>
              {gemsLoading ? '…' : `${gems.length} active`}
            </span>
          </div>

          <div style={{ overflowY: 'auto', flex: 1 }}>
            {gemsLoading ? (
              <div style={{ padding: 20, textAlign: 'center', color: 'var(--text3)', fontSize: 11 }}>
                Loading…
              </div>
            ) : gems.length === 0 ? (
              <div style={{ padding: 20, textAlign: 'center', color: 'var(--text3)', fontSize: 11,
                            lineHeight: 1.6 }}>
                No active gems.<br />Research a theme above<br />to get started.
              </div>
            ) : (
              gems.map(g => (
                <GemCard key={g.stock_code} gem={g}
                  selected={selectedGem?.stock_code === g.stock_code && rightMode === 'detail'}
                  onClick={() => {
                    setSelectedGem(g)
                    setCandidates([])
                    setResearchErr('')
                    setAddMsg('')
                  }} />
              ))
            )}
          </div>
        </div>

        {/* Right panel */}
        <div style={{ flex: 1, minWidth: 0, background: 'var(--bg)', overflow: 'hidden',
                      display: 'flex', flexDirection: 'column' }}>
          {rightMode === 'searching' && <SearchingState />}
          {rightMode === 'candidates' && (
            <CandidatesPanel
              candidates={candidates}
              selected={selected}
              onToggle={toggleCandidate}
              onAdd={handleAddSelected}
              onClear={() => { setCandidates([]); setSelected(new Set()) }}
              addMsg={addMsg}
              err={researchErr}
            />
          )}
          {rightMode === 'detail' && selectedGem && (
            <GemDetail gem={selectedGem} onRemove={setRemoveTarget} />
          )}
          {rightMode === 'empty' && <EmptyState />}
        </div>
      </div>

      {removeTarget && (
        <RemoveModal gem={removeTarget}
          onConfirm={handleRemove}
          onCancel={() => setRemoveTarget(null)} />
      )}

      <style>{`
        @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }
      `}</style>
    </div>
  )
}
