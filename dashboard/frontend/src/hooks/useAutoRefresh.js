import { useState, useEffect, useCallback } from 'react'

export function useAutoRefresh(url, intervalMs = 60000) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const [loading, setLoading] = useState(true)
  const [lastUpdated, setLastUpdated] = useState(null)

  const fetchData = useCallback(() => {
    fetch(url)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        return r.json()
      })
      .then(d => {
        setData(d)
        setError(null)
        setLoading(false)
        setLastUpdated(new Date().toLocaleTimeString())
      })
      .catch(e => {
        setError(e.message)
        setLoading(false)
      })
  }, [url])

  useEffect(() => {
    fetchData()
    const id = setInterval(fetchData, intervalMs)
    return () => clearInterval(id)
  }, [fetchData, intervalMs])

  return { data, error, loading, lastUpdated, refresh: fetchData }
}
