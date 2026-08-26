import { createContext, useContext, useEffect, useRef, useState } from 'react'
import { useAuth } from './auth.jsx'
import { ingestAiEventLog, pushLogOnce } from './aiLogs.jsx'

const WSContext = createContext(null)

function tokenExpiresSoon(token, leewaySeconds = 30) {
  try {
    const encoded = token.split('.')[1]
    const normalized = encoded.replace(/-/g, '+').replace(/_/g, '/')
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')
    const payload = JSON.parse(atob(padded))
    return !payload.exp || payload.exp * 1000 <= Date.now() + leewaySeconds * 1000
  } catch (err) {
    return true
  }
}

export function WSProvider({ children }) {
  const { token, refreshSession } = useAuth()
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
      // 业务仅支持全场大小球：保留 total 盘口的 under/over 两边。
      const total = data.odds.find((o) => String(o.bet_type || '').toLowerCase() === 'total')
      if (total?.odds_data) {
        setOddsUpdates((prev) => ({
          ...prev,
          [mid]: { ...total.odds_data, _updatedAt: Date.now() },
        }))
      }
    }
  }

  useEffect(() => {
    if (!token) {
      setConnected(false)
      return undefined
    }

    let disposed = false
    let connecting = false

    const scheduleReconnect = (nextToken, delay) => {
      if (disposed || !nextToken) return
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      reconnectTimer.current = setTimeout(() => connect(nextToken), delay)
    }

    const connect = async (candidateToken) => {
      if (disposed || connecting || !candidateToken) return
      connecting = true
      let wsToken = candidateToken
      if (tokenExpiresSoon(wsToken)) {
        wsToken = await refreshSession()
      }
      connecting = false
      if (disposed || !wsToken) return

      const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws/odds`
      // Token 通过 WebSocket 子协议头传递，避免 URL / 代理访问日志泄露 JWT。
      const ws = new WebSocket(wsUrl, ['access-token', wsToken])
      wsRef.current = ws

      ws.onopen = () => {
        if (disposed) {
          ws.close()
          return
        }
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
            // snapshot received
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

      ws.onclose = async (event) => {
        if (wsRef.current === ws) wsRef.current = null
        setConnected(false)
        if (disposed) return

        pushLogOnce('ws:disconnected', 'engine', '实时连接已断开，系统将自动重连', null, 10000)
        const shouldRefresh = event.code === 4001 || tokenExpiresSoon(wsToken, 0)
        if (shouldRefresh) {
          const refreshedToken = await refreshSession()
          if (refreshedToken) scheduleReconnect(refreshedToken, 1000)
          return
        }
        scheduleReconnect(wsToken, 5000)
      }

      ws.onerror = (err) => {
        console.error('[WS] Error:', err)
      }
    }

    connect(token)
    return () => {
      disposed = true
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current)
      reconnectTimer.current = null
      if (wsRef.current) {
        wsRef.current.close()
        wsRef.current = null
      }
    }
  }, [token, refreshSession])

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
