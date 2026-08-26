import { useState, useEffect } from 'react'

/**
 * AI 日志共享 store（localStorage 持久化）
 * AIPanel 写入日志，Logs 页面读取展示
 */

const MAX_LOGS = 300
const STORAGE_KEY = 'ai_logs_v1'
const RECENT_KEY_TTL_MS = 15000

// 模块级存储 + 订阅
let _logs = []
let _loaded = false
const _subscribers = new Set()
const _recentKeys = new Map()

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

function _pruneRecentKeys(now = Date.now()) {
  for (const [key, expiresAt] of _recentKeys.entries()) {
    if (expiresAt <= now) _recentKeys.delete(key)
  }
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

export function pushLogOnce(uniqueKey, type, message, detail = null, ttlMs = RECENT_KEY_TTL_MS) {
  _load()
  const key = String(uniqueKey || '').trim()
  if (!key) {
    pushLog(type, message, detail)
    return
  }
  const now = Date.now()
  _pruneRecentKeys(now)
  const expiresAt = _recentKeys.get(key) || 0
  if (expiresAt > now) return
  _recentKeys.set(key, now + Math.max(1000, Number(ttlMs) || RECENT_KEY_TTL_MS))
  pushLog(type, message, detail)
}

function _num(value, fallback = 0) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function _fmtOdds(value) {
  const n = _num(value, 0)
  return n > 0 ? n.toFixed(2) : '-'
}

function _fmtWinRate(value) {
  const n = _num(value, 0)
  return `${n.toFixed(0)}%`
}

function _analysisTag(item) {
  if (item?.should_bet) return '✓推荐'
  if (item?.status === 'skipped' || item?.selection === 'skip' || !item?.selection) return '跳过'
  return _fmtWinRate(item?.confidence)
}

function _shortReason(value) {
  const text = String(value || '').replace(/^\[不投注\]\s*/u, '').replace(/\s+/g, ' ').trim()
  return text ? text.slice(0, 100) : ''
}

function _providerLabel(provider, fallback = '') {
  const p = String(provider || fallback || '').trim()
  return p || ''
}

export function ingestAiEventLog(detail) {
  _load()
  const eventType = String(detail?.type || '').trim()
  if (!eventType) return
  const data = detail?.data || {}
  const stamp = String(detail?.timestamp || data?.timestamp || new Date().toISOString())
  const eventKey = `${eventType}:${stamp}`

  if (eventType === 'ai_cycle_start') {
    const candidates = _num(data?.candidates, 0)
    const groups = _num(data?.fixture_groups, 0)
    const mode = data?.auto_place ? '自动' : '人工'
    pushLogOnce(eventKey, 'cycle', `AI 开始新一轮分析：${candidates} 场候选 / ${groups} 组同场（${mode}模式）`, data)
    return
  }

  if (eventType === 'ai_analysis_done') {
    const skipped = data?.status === 'skipped' || data?.selection === 'skip' || !data?.selection
    const sel = skipped ? '跳过' : String(data.selection)
    const market = String(data?.bet_type || 'total')
    const marketLabel = market === 'total' ? '全场大小球' : market || '-'
    const finalPct = _num(data?.final_calibrated_win_rate, _num(data?.confidence, 0) * 100)
    const requiredPct = _num(data?.required_win_rate, 0)
    const tag = skipped
      ? '跳过'
      : (data?.should_bet ? `✓推荐 ${_fmtWinRate(finalPct)}` : _fmtWinRate(finalPct))
    const threshold = !skipped && requiredPct > 0 ? `/门槛${_fmtWinRate(requiredPct)}` : ''
    const reason = (skipped || data?.status === 'rejected') ? _shortReason(data?.reasoning) : ''
    pushLogOnce(
      eventKey,
      'analysis',
      `${data?.home_team || '-'} vs ${data?.away_team || '-'}  ${marketLabel} ${sel}${skipped ? '' : ` @ ${_fmtOdds(data?.odds)}`}  ${tag}${threshold}${reason ? ` · ${reason}` : ''}`,
      data,
    )
    return
  }

  if (eventType === 'ai_cycle_complete') {
    const analyzed = _num(data?.analyzed, 0)
    const executed = _num(data?.executed, 0)
    pushLogOnce(eventKey, 'cycle', `AI 完成一轮分析：${analyzed} 场分析，${executed} 笔下单`, data)
    for (const item of Array.isArray(data?.analysis_summary) ? data.analysis_summary : []) {
      const key = `${eventKey}:analysis:${item.match_id || `${item.home_team || ''}-${item.away_team || ''}`}:${item.selection || ''}:${item.bet_type || ''}`
      const market = String(item.bet_type || '')
      const marketLabel = market === 'total' ? '全场大小球' : market || '-'
      const skipped = item.status === 'skipped' || item.selection === 'skip' || !item.selection
      const selection = skipped ? '跳过' : String(item.selection)
      const tag = _analysisTag(item)
      const reason = skipped ? _shortReason(item.reasoning) : ''
      pushLogOnce(
        key,
        'analysis',
        `${item.home_team || '-'} vs ${item.away_team || '-'}  ${marketLabel} ${selection}${skipped ? '' : ` @ ${_fmtOdds(item.odds)}`}  ${tag}${reason ? ` · ${reason}` : ''}`,
        item,
      )
    }
    return
  }

  if (eventType === 'ai_cycle_done') {
    const analyzed = _num(data?.analyzed, 0)
    pushLogOnce(eventKey, 'cycle', `AI 分析完成：${analyzed} 场，无符合策略的下单机会`, data)
    for (const item of Array.isArray(data?.analysis_summary) ? data.analysis_summary : []) {
      const key = `${eventKey}:analysis:${item.match_id || `${item.home_team || ''}-${item.away_team || ''}`}:${item.selection || ''}:${item.bet_type || ''}`
      const market = String(item.bet_type || '')
      const marketLabel = market === 'total' ? '全场大小球' : market || '-'
      const skipped = item.status === 'skipped' || item.selection === 'skip' || !item.selection
      const selection = skipped ? '跳过' : String(item.selection)
      const tag = _analysisTag(item)
      const reason = skipped ? _shortReason(item.reasoning) : ''
      pushLogOnce(
        key,
        'analysis',
        `${item.home_team || '-'} vs ${item.away_team || '-'}  ${marketLabel} ${selection}${skipped ? '' : ` @ ${_fmtOdds(item.odds)}`}  ${tag}${reason ? ` · ${reason}` : ''}`,
        item,
      )
    }
    return
  }

  if (eventType === 'ai_risk_stop') {
    pushLogOnce(eventKey, 'risk', `风控触发: ${typeof data === 'string' ? data : (data?.message || 'AI 引擎已暂停')}`, data)
    return
  }

  if (eventType === 'ai_manual_recommend') {
    const sel = String(data?.selection || '-')
    const provider = _providerLabel(data?.provider)
    const providerText = provider ? ` · ${provider}` : ''
    pushLogOnce(eventKey, 'recommend', `推荐: ${sel} @ ${_fmtOdds(data?.odds)}${providerText}`, data)
    return
  }

  if (eventType === 'ai_recs_ready') {
    const sport = String(data?.sport || '').trim()
    const provider = _providerLabel(data?.provider)
    const count = _num(data?.count, 0)
    const fixtures = _num(data?.fixtures, 0)
    const scope = [sport, provider].filter(Boolean).join(' · ')
    const suffix = fixtures > 0 ? `，覆盖 ${fixtures} 场比赛` : ''
    pushLogOnce(eventKey, 'analysis', `推荐刷新完成：${count} 场可展示${scope ? `（${scope}）` : ''}${suffix}`, data)
    return
  }

  if (eventType === 'ai_config_updated') {
    pushLogOnce(
      eventKey,
      'config',
      `AI 设置已更新：置信度≥${_fmtWinRate(_num(data?.min_confidence, 0) * 100)} · 赔率 ${String(data?.min_odds ?? '-')}~${String(data?.max_odds ?? '-')}`,
      data,
    )
    return
  }

  if (eventType === 'ai_bet_placed') {
    const provider = _providerLabel(data?.provider)
    const suffix = provider ? ` · ${provider}` : ''
    pushLogOnce(
      eventKey,
      'bet_placed',
      `下单成功: ${String(data?.selection || '-')} @ ${_fmtOdds(data?.odds)}${suffix}`,
      data,
    )
    return
  }

  if (eventType === 'ai_bet_failed') {
    pushLogOnce(eventKey, 'bet_failed', `下单失败: ${String(data?.message || '未知原因')}`, data)
    return
  }

  if (eventType === 'ai_monitor') {
    const m = data?.matches || {}
    const o = data?.odds || {}
    const engineOn = data?.engine?.running
    const issues = Array.isArray(data?.issues) ? data.issues : []
    const errCount = issues.filter((i) => i?.level === 'error').length
    const summary = String(data?.summary || '')
    const msg = `[实时监控] 比赛=${_num(m.total, 0)} (OB:${_num(m.ob, 0)}/平博:${_num(m.pinnacle, 0)}) 赔率=${_num(o.total, 0)} TOTAL齐全=${_num(o.total_complete, 0)} | 引擎=${engineOn ? '运行中' : '未运行'} | ${summary}`
    if (errCount > 0) {
      // 存在错误：每轮立即记录
      pushLogOnce(eventKey, 'monitor', msg, data)
    } else {
      // 正常心跳：5 分钟一条，避免刷屏（日志上限 300 条）
      const bucket = Math.floor(Date.parse(stamp) / 300000)
      pushLogOnce(`ai_monitor:hb:${bucket}`, 'monitor', msg, data, 60 * 60 * 1000)
    }
    return
  }

  if (eventType === 'ai_prefetch_done') {
    const football = _num(data?.football, 0)
    const basketball = _num(data?.basketball, 0)
    const elapsed = _num(data?.elapsed_sec, 0)
    const source = data?.source === 'manual' ? '手动' : '定时'
    if (data?.ok) {
      pushLogOnce(
        eventKey,
        'prefetch',
        `捷报数据预取完成（${source}）：足球 ${football} 场 / 篮球 ${basketball} 场${elapsed > 0 ? ` · 耗时 ${elapsed}s` : ''}`,
        data,
      )
    } else {
      pushLogOnce(
        eventKey,
        'prefetch',
        `捷报数据预取失败（${source}）：${String(data?.error || '未知错误')}`,
        data,
      )
    }
    return
  }
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
