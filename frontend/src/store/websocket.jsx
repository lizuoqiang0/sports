import { createContext, useContext, useEffect, useRef, useState } from 'react'
import { useAuth } from './auth.jsx'
import { ingestAiEventLog, pushLogOnce } from './aiLogs.jsx'

const WSContext = createContext(null)

export function WSProvider({ children }) {
  const { token } = useAuth()
  const [connected, setConnected] = useState(false)
  const [oddsUpdates, setOddsUpdates] = useState({})
  const [matchUpdates, setMatchUpdates] = useState({})
  const wsRef = useRef(null)
  const reconnectTimer = useRef(null)

  const applyMatchUpdate = (data) => {
    if (!data?.match_id) return
    const mid = data.match_id
    setMatchUpdates((prev) => ({
      ...prev,
      [mid]: {
        ...prev[mid],
        ...data,
        _updatedAt: Date.now(),
      },
    }))
    if (data.odds_data) {
      setOddsUpdates((prev) => ({
        ...prev,
        [mid]: { ...data.odds_data, _updatedAt: Date.now() },
      }))
    } else if (Array.isArray(data.odds)) {
      const ml = data.odds.find((o) => o.bet_type === 'moneyline' || o.bet_type === 'Moneyline')
      if (ml?.odds_data) {
        setOddsUpdates((prev) => ({
          ...prev,
          [mid]: { ...ml.odds_data, _updatedAt: Date.now() },
        }))
      }
    }
  }

  const connect = () => {
    if (!token) return

    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/odds?token=${token}`
    const ws = new WebSocket(wsUrl)
    wsRef.current = ws

    ws.onopen = () => {
      console.log('[WS] Connected')
      setConnected(true)
      pushLogOnce('ws:connected', 'engine', '实时连接已建立，开始接收 AI / 赔率事件', null, 10000)
      ws.send(JSON.stringify({ action: 'ping' }))
      // 订阅全站滚球推送
      ws.send(JSON.stringify({ action: 'subscribe', channel: 'odds:live' }))
    }

    ws.onmessage = (e) => {
      try {
        const msg = JSON.parse(e.data)

        if (msg.type === 'match_update' && msg.data) {
          applyMatchUpdate(msg.data)
          window.dispatchEvent(new CustomEvent('matchUpdate', { detail: msg.data }))
        } else if (msg.type === 'odds_update' && msg.data) {
          const { match_id, odds_data } = msg.data
          setOddsUpdates((prev) => ({
            ...prev,
            [match_id]: {
              ...odds_data,
              _updatedAt: Date.now(),
            },
          }))
          if (msg.data.home_score != null || msg.data.clock) {
            applyMatchUpdate(msg.data)
          }
        } else if (msg.type === 'match_status' && msg.data) {
          applyMatchUpdate(msg.data)
        } else if (msg.type === 'snapshot' && msg.data) {
          console.log('[WS] Snapshot:', msg.data)
        } else if (msg.type === 'bet_placed') {
          const data = msg.data || {}
          pushLogOnce(
            `bet_placed:${data.bet_id || data.match_id || ''}:${data.created_at || ''}`,
            'bet_placed',
            `手动下单成功: ${String(data.selection || '-')} @ ${Number(data.odds || 0).toFixed(2)}`,
            data,
          )
          window.dispatchEvent(new CustomEvent('betUpdate', { detail: msg.data }))
        } else if (msg.type === 'ai_recommend' || (msg.type && String(msg.type).startsWith('ai_'))) {
          ingestAiEventLog(msg)
          window.dispatchEvent(new CustomEvent('aiUpdate', { detail: msg }))
        }
      } catch (err) {
        console.error('[WS] Parse error:', err)
      }
    }

    ws.onclose = () => {
      console.log('[WS] Disconnected')
      setConnected(false)
      pushLogOnce('ws:disconnected', 'engine', '实时连接已断开，系统将自动重连', null, 10000)
      reconnectTimer.current = setTimeout(connect, 5000)
    }

    ws.onerror = (err) => {
      console.error('[WS] Error:', err)
    }
  }

  useEffect(() => {
    if (token) {
      connect()
    }
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      if (wsRef.current) wsRef.current.close()
    }
  }, [token])

  const subscribe = (matchId) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'subscribe',
        match_id: matchId,
      }))
    }
  }

  const subscribeSport = (sport) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({
        action: 'subscribe_sport',
        sport,
      }))
    }
  }

  return (
    <WSContext.Provider value={{
      connected,
      oddsUpdates,
      matchUpdates,
      subscribe,
      subscribeSport,
      ws: wsRef.current,
    }}>
      {children}
    </WSContext.Provider>
  )
}

export function useWebSocket() {
  const ctx = useContext(WSContext)
  if (!ctx) throw new Error('useWebSocket must be used within WSProvider')
  return ctx
}
