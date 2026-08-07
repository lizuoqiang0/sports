import { useState, useEffect } from 'react'

/**
 * AI 日志共享 store（localStorage 持久化）
 * AIPanel 写入日志，Logs 页面读取展示
 */

const MAX_LOGS = 300
const STORAGE_KEY = 'ai_logs_v1'

// 模块级存储 + 订阅
let _logs = []
let _loaded = false
const _subscribers = new Set()

function _load() {
  if (_loaded) return
  _loaded = true
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    if (raw) {
      const arr = JSON.parse(raw)
      if (Array.isArray(arr)) {
        _logs = arr.slice(0, MAX_LOGS)
      }
    }
  } catch {
    // ignore parse errors
  }
}

function _persist() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(_logs))
  } catch {
    // localStorage 满了或不可用，静默忽略
  }
}

function emit() {
  const snapshot = [..._logs]
  _subscribers.forEach((fn) => {
    try {
      fn(snapshot)
    } catch {
      // 单个订阅者报错不影响其他
    }
  })
}

function _safeDetail(detail) {
  if (!detail) return null
  if (typeof detail === 'string') return detail.slice(0, 500)
  try {
    const str = JSON.stringify(detail)
    return str.length > 500 ? str.slice(0, 500) + '...' : str
  } catch {
    return '[unserializable]'
  }
}

export function pushLog(type, message, detail = null) {
  _load()
  const entry = {
    id: Date.now() + '_' + Math.random().toString(36).slice(2, 8),
    time: new Date().toISOString(),
    type,
    message: typeof message === 'string' ? message : String(message || ''),
    detail: _safeDetail(detail),
  }
  _logs = [entry, ..._logs].slice(0, MAX_LOGS)
  _persist()
  emit()
}

export function clearLogs() {
  _logs = []
  _persist()
  emit()
}

export function useAiLogs() {
  _load()
  const [logs, setLogs] = useState(_logs)

  useEffect(() => {
    const fn = (snap) => setLogs(snap)
    _subscribers.add(fn)
    setLogs([..._logs])
    return () => _subscribers.delete(fn)
  }, [])

  return logs
}
