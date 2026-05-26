import { useState, useEffect } from 'react'

function _isDST(now) {
  // US DST: second Sunday in March → first Sunday in November
  const yr = now.getUTCFullYear()
  const m  = now.getUTCMonth() + 1  // 1-12

  function nthSunday(month, n) {
    // Return UTC day-of-month of the nth Sunday in `month` of `yr`
    const d = new Date(Date.UTC(yr, month - 1, 1))
    let count = 0
    while (true) {
      if (d.getUTCDay() === 0) { count++; if (count === n) return d.getUTCDate() }
      d.setUTCDate(d.getUTCDate() + 1)
    }
  }

  const dstStart = new Date(Date.UTC(yr, 2,  nthSunday(3,  2), 7))  // Mar 2nd Sun 02:00 ET = 07:00 UTC
  const dstEnd   = new Date(Date.UTC(yr, 10, nthSunday(11, 1), 6))  // Nov 1st Sun 02:00 ET = 06:00 UTC
  return now >= dstStart && now < dstEnd
}

function _usMarketOpen(now) {
  const dow = now.getUTCDay()
  if (dow === 0 || dow === 6) return false  // weekend

  const dst = _isDST(now)
  const utcMin = now.getUTCHours() * 60 + now.getUTCMinutes()
  const open  = dst ? 13 * 60 + 30 : 14 * 60 + 30   // 9:30 ET in UTC
  const close = dst ? 20 * 60      : 21 * 60          // 16:00 ET in UTC
  return utcMin >= open && utcMin < close
}

function _hkMarketOpen(now) {
  // HKT = UTC+8, no DST
  const hkt = new Date(now.getTime() + 8 * 3600 * 1000)
  const dow  = hkt.getUTCDay()
  if (dow === 0 || dow === 6) return false

  const hktMin = hkt.getUTCHours() * 60 + hkt.getUTCMinutes()
  const morning   = hktMin >= 9 * 60 + 30 && hktMin < 12 * 60   // 09:30-12:00
  const afternoon = hktMin >= 13 * 60      && hktMin < 16 * 60   // 13:00-16:00
  return morning || afternoon
}

function _formatClock(now) {
  const pad2 = n => String(n).padStart(2, '0')
  return `${pad2(now.getHours())}:${pad2(now.getMinutes())}:${pad2(now.getSeconds())}`
}

export function useMarketStatus() {
  const [state, setState] = useState(() => {
    const now = new Date()
    return {
      usOpen:  _usMarketOpen(now),
      hkOpen:  _hkMarketOpen(now),
      clock:   _formatClock(now),
    }
  })

  useEffect(() => {
    const id = setInterval(() => {
      const now = new Date()
      setState({
        usOpen:  _usMarketOpen(now),
        hkOpen:  _hkMarketOpen(now),
        clock:   _formatClock(now),
      })
    }, 1000)
    return () => clearInterval(id)
  }, [])

  return state
}
