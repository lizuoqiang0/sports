import { useState, useEffect, useCallback, useRef } from 'react'
import { aiAPI, betsAPI, adminAPI } from '../lib/api.js'
import { formatAiRecommendationReason } from '../lib/aiReasoning.js'
import { SITE_NAMES, SITE_ORDER } from '../lib/sites.js'
import { formatIntervalLabel } from '../lib/uiCopy.js'
import { useAuth } from '../store/auth.jsx'
import { usePagePoll } from '../hooks/usePagePoll.js'
import { pushLog as addAiLog } from '../store/aiLogs.jsx'
import PageHeader from '../components/PageHeader.jsx'
import BetModeSwitch from '../components/BetModeSwitch.jsx'
import toast from 'react-hot-toast'
import {
  Bot, Play, Square, Settings, Loader2, Shield,
  AlertTriangle, Zap, Brain,
  ChevronDown, ChevronUp, Database, Power, Download, Upload, RefreshCw, CheckSquare, Trash2,
} from 'lucide-react'

const SEL_LABEL = {
  under: '小球',
  home: '主',
  away: '客',
  draw: '平',
}

const MARKET_LABEL = {
  total: '全场小球',
  moneyline: '独赢',
  spread: '亚洲让球',
}

const SPORT_TABS = [
  { key: 'football', label: '足球' },
  { key: 'basketball', label: '篮球' },
]

const SITE_TABS = SITE_ORDER.map((code) => ({ key: code, label: SITE_NAMES[code] }))
const ALIAS_SPORT_TABS = [{ key: 'all', label: '全部' }, ...SPORT_TABS]
const CANDIDATE_SORTS = [
  { key: 'score_desc', label: '分数从高到低' },
  { key: 'count_desc', label: '出现次数从多到少' },
  { key: 'recent_desc', label: '最近出现优先' },
]
const OVERRIDE_SORTS = [
  { key: 'recent_desc', label: '最近批准优先' },
  { key: 'score_desc', label: '分数从高到低' },
  { key: 'name_asc', label: '名称排序' },
]

const DEFAULT_FORM_DATA = {
  is_active: false,
  max_bet_amount: 50,
  max_daily_bets: 3,
  preferred_sports: [],
  excluded_teams: [],
  stop_loss: 500,
  take_profit: 1000,
  min_confidence: 0.6,
  min_odds: 1.6,
  max_odds: 5.5,
}

function toFiniteNumber(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

function normalizeFormData(source, fallback = DEFAULT_FORM_DATA) {
  const base = { ...DEFAULT_FORM_DATA, ...fallback }
  const data = source || {}
  return {
    is_active: data.is_active ?? base.is_active,
    max_bet_amount: toFiniteNumber(data.max_bet_amount, base.max_bet_amount),
    max_daily_bets: toFiniteNumber(data.max_daily_bets, base.max_daily_bets),
    preferred_sports: Array.isArray(data.preferred_sports) ? data.preferred_sports : base.preferred_sports,
    excluded_teams: Array.isArray(data.excluded_teams) ? data.excluded_teams : base.excluded_teams,
    stop_loss: toFiniteNumber(data.stop_loss, base.stop_loss),
    take_profit: toFiniteNumber(data.take_profit, base.take_profit),
    min_confidence: toFiniteNumber(data.min_confidence, base.min_confidence),
    min_odds: toFiniteNumber(data.min_odds, base.min_odds),
    max_odds: toFiniteNumber(data.max_odds, base.max_odds),
  }
}

function formatCellLine(market, cell) {
  const line = market?.line
  const sel = cell?.selection
  if (sel === 'under') {
    return line != null && line !== '' ? `小球 ${line}` : '小球 -'
  }
  if (String(market?.bet_type || '') === 'spread' && line != null && line !== '') {
    const n = Number(line)
    if (!Number.isFinite(n)) return String(line)
    // 库内 line 为主队视角：主显示原值，客显示反向
    if (sel === 'home') return n > 0 ? `+${n}` : String(n)
    if (sel === 'away') {
      const opp = -n
      return opp > 0 ? `+${opp}` : String(opp)
    }
  }
  if (String(market?.bet_type || '') === 'moneyline') {
    return SEL_LABEL[sel] || sel || '-'
  }
  return SEL_LABEL[sel] || sel || '-'
}

function formatOpeningHint(market) {
  const open = market?.opening_line
  const move = market?.line_movement
  if (open == null && !move) return null
  const parts = []
  if (open != null && open !== '') parts.push(`初 ${open}`)
  const delta = move?.line_delta
  if (delta != null && Number(delta) !== 0) {
    const n = Number(delta)
    parts.push(n > 0 ? `升 +${n}` : `降 ${n}`)
  } else if (move?.change_count > 0) {
    parts.push(`变 ${move.change_count}次`)
  }
  return parts.length ? parts.join(' · ') : null
}

function formatDateTime(value) {
  if (!value) return '-'
  try {
    return new Date(value).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return String(value)
  }
}

function downloadJson(filename, data) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}

function parseAliasImportDraft(text) {
  if (!String(text || '').trim()) {
    return { items: [], error: '' }
  }
  try {
    const parsed = JSON.parse(text)
    const items = Array.isArray(parsed) ? parsed : parsed.items
    if (!Array.isArray(items)) {
      return { items: [], error: 'JSON 中缺少 items 数组' }
    }
    return { items, error: '' }
  } catch {
    return { items: [], error: 'JSON 格式无效' }
  }
}

export default function AIPanelPage() {
  const { updateUser } = useAuth()
  const [engineStatus, setEngineStatus] = useState(null)
  const [config, setConfig] = useState(null)
  const [recommendations, setRecommendations] = useState([])
  const [loading, setLoading] = useState(true)
  const [recsLoading, setRecsLoading] = useState(false)
  const [starting, setStarting] = useState(false)
  const [showSettings, setShowSettings] = useState(false)
  const [betMode, setBetMode] = useState('manual')
  const [stakeByMatch, setStakeByMatch] = useState({})
  const [minBetAmount, setMinBetAmount] = useState(1)
  const [bettingId, setBettingId] = useState(null)
  const [sportTab, setSportTab] = useState('football')
  const [siteTab, setSiteTab] = useState(SITE_ORDER[0] || 'ob')
  const [recsMeta, setRecsMeta] = useState({
    status: 'idle', progress: 0, total: 0, hint: '', filterHint: '', minWinRate: null, rawCount: 0,
    analysisEnabled: false,
  })
  const [analysisBusy, setAnalysisBusy] = useState(false)
  const [dsEnabled, setDsEnabled] = useState(null)
  const [dsMeta, setDsMeta] = useState(null)
  const [dsLoading, setDsLoading] = useState(false)
  const [prefetchProgress, setPrefetchProgress] = useState(null)
  const [prefetchNullCount, setPrefetchNullCount] = useState(0)
  const [aliasCandidates, setAliasCandidates] = useState([])
  const [aliasOverrides, setAliasOverrides] = useState([])
  const [aliasAuditLogs, setAliasAuditLogs] = useState([])
  const [aliasLoading, setAliasLoading] = useState(false)
  const [selectedCandidateIds, setSelectedCandidateIds] = useState([])
  const [selectedOverrideIds, setSelectedOverrideIds] = useState([])
  const [importText, setImportText] = useState('')
  const [importLoading, setImportLoading] = useState(false)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [importPreview, setImportPreview] = useState(null)
  const [aliasSportFilter, setAliasSportFilter] = useState('all')
  const [candidateSort, setCandidateSort] = useState('score_desc')
  const [overrideSort, setOverrideSort] = useState('recent_desc')
  const [candidateSearch, setCandidateSearch] = useState('')
  const [overrideSearch, setOverrideSearch] = useState('')
  const [minScoreFilter, setMinScoreFilter] = useState('')
  const [maxScoreFilter, setMaxScoreFilter] = useState('')
  const [highScoreThreshold, setHighScoreThreshold] = useState(0.6)
  const prefetchTimerRef = useRef(null)

  const loadDsSwitch = async () => {
    try {
      const res = await adminAPI.getDataSourceSwitch()
      setDsEnabled(res.data?.enabled ?? false)
      setDsMeta(res.data || null)
    } catch {
      setDsEnabled(false)
      setDsMeta(null)
    }
  }

  // 挂载时检查是否有正在运行的预取，有则恢复进度条
  const checkActivePrefetch = async () => {
    try {
      const res = await adminAPI.getPrefetchProgress()
      const p = res.data
      if (p && p.total > 0 && p.done < p.total) {
        setPrefetchProgress({ ...p, elapsed: Date.now() / 1000 - (p.started_at || 0) })
        clearInterval(prefetchTimerRef.current)
        prefetchTimerRef.current = setInterval(pollProgress, 500)
      }
    } catch {
      // ignore
    }
  }

  const loadAliasData = async ({ silent = false } = {}) => {
    if (!silent) setAliasLoading(true)
    try {
      const [candRes, overrideRes, auditRes] = await Promise.all([
        adminAPI.getAliasCandidates('all', 100, 0),
        adminAPI.getAliasOverrides('all', 100),
        adminAPI.getAliasAuditLogs(30),
      ])
      const nextCandidates = candRes.data?.items || []
      const nextOverrides = overrideRes.data?.items || []
      setAliasCandidates(nextCandidates)
      setAliasOverrides(nextOverrides)
      setAliasAuditLogs(auditRes.data?.items || [])
      setSelectedCandidateIds((prev) => prev.filter((id) => nextCandidates.some((item) => item.id === id)))
      setSelectedOverrideIds((prev) => prev.filter((id) => nextOverrides.some((item) => item.id === id)))
    } catch (e) {
      if (!silent) toast.error(e?.message || '别名清单加载失败')
    } finally {
      if (!silent) setAliasLoading(false)
    }
  }

  const toggleDs = async () => {
    const next = !dsEnabled
    setDsLoading(true)
    try {
      await adminAPI.setDataSourceSwitch(next)
      setDsEnabled(next)
      if (next) {
        addAiLog('prefetch', '数据源已开启，开始预取当日赛事数据')
        // 开启后自动触发预取 + 进度条
        await adminAPI.triggerPrefetch('all')
        addAiLog('prefetch', '预取任务已触发：足球 + 篮球')
        setPrefetchNullCount(0)
        setPrefetchProgress({ total: 1, done: 0, elapsed: 0, sport: '启动中...' })
        clearInterval(prefetchTimerRef.current)
        prefetchTimerRef.current = setInterval(pollProgress, 500)
        await pollProgress()
      } else {
        addAiLog('prefetch', '数据源已关闭')
      }
      await loadDsSwitch()
    } catch (e) {
      toast.error(e.message || '操作失败')
    } finally {
      setDsLoading(false)
    }
  }

  const _lastLoggedSport = useRef(null)
  const pollProgress = async () => {
    try {
      const res = await adminAPI.getPrefetchProgress()
      const p = res.data
      if (p && p.total > 0) {
        setPrefetchNullCount(0)
        const elapsed = Date.now() / 1000 - (p.started_at || 0)
        setPrefetchProgress({ ...p, elapsed })
        // 运动切换时记录日志
        if (_lastLoggedSport.current !== p.sport) {
          _lastLoggedSport.current = p.sport
          addAiLog('prefetch', `开始爬取 ${p.sport === 'football' ? '足球' : '篮球'}：共 ${p.total} 场`)
        }
        if (p.done >= p.total) {
          clearInterval(prefetchTimerRef.current)
          prefetchTimerRef.current = null
          setTimeout(() => setPrefetchProgress(null), 3000)
          addAiLog('prefetch', `${p.sport === 'football' ? '足球' : '篮球'}爬取完成：${p.total} 场，耗时 ${Math.round(elapsed)}s`)
        }
      } else {
        // 后端获取赛程列表需要 ~20s，容忍 60 次（~30s）空响应
        setPrefetchNullCount(c => {
          if (c >= 60) {
            clearInterval(prefetchTimerRef.current)
            prefetchTimerRef.current = null
            setPrefetchProgress(null)
            return 0
          }
          return c + 1
        })
      }
    } catch {
      // ignore
    }
  }

  const positionMode = 'single'
  const analyzing = recsMeta.status === 'analyzing' || recsMeta.status === 'starting'
  const analysisOn = !!recsMeta.analysisEnabled || analyzing

  // 设置表单
  const [formData, setFormData] = useState(DEFAULT_FORM_DATA)

  useEffect(() => {
    loadAll()
    loadDsSwitch()
    checkActivePrefetch()
    loadAliasData()
    // 组件卸载时清理轮询，但不停止后端任务
    return () => {
      clearInterval(prefetchTimerRef.current)
      prefetchTimerRef.current = null
    }
  }, [])

  // 监听AI通知
  useEffect(() => {
    const handleAIUpdate = (e) => {
      const detail = e.detail
      if (detail.type === 'ai_cycle_complete') {
        const n = detail.data?.executed || 0
        const analyzed = detail.data?.analyzed || 0
        toast.success(`本轮完成：分析 ${analyzed} 场，下单 ${n} 笔`)
        loadRecommendations(false)
      } else if (detail.type === 'ai_cycle_done') {
        const analyzed = detail.data?.analyzed || 0
        toast(`本轮完成：分析 ${analyzed} 场，未下单`, { icon: '📊' })
      } else if (detail.type === 'ai_risk_stop') {
        toast.error(`AI 已暂停：${detail.data}`)
        setEngineStatus({ running: false })
      } else if (detail.type === 'ai_manual_recommend') {
        toast(`人工推荐：${detail.data.selection} @ ${detail.data.odds}`, { icon: '🤖' })
        loadRecommendations(false)
      } else if (detail.type === 'ai_recs_ready') {
        loadRecommendations(false)
      } else if (detail.type === 'ai_config_updated') {
        toast.success('AI 设置已更新')
        const d = detail.data || {}
        setConfig((prev) => ({ ...(prev || {}), ...d }))
        setFormData((prev) => normalizeFormData({ ...prev, ...d }, prev))
        loadRecommendations(true)
      } else if (detail.type === 'ai_bet_placed') {
        toast.success(`下单成功：${detail.data.selection} @ ${detail.data.odds}`)
      } else if (detail.type === 'ai_bet_failed') {
        toast.error(detail.data?.message || '下单失败')
      }
    }
    window.addEventListener('aiUpdate', handleAIUpdate)
    return () => window.removeEventListener('aiUpdate', handleAIUpdate)
  }, [])

  const loadRecommendations = useCallback(async (refresh = true, sport = sportTab, provider = siteTab, { silent = false } = {}) => {
    if (!silent) setRecsLoading(true)
    try {
      const recsRes = await aiAPI.recommendations(sport, 80, refresh, provider)
      const data = recsRes.data || {}
      const list = data.recommendations || []
      setRecommendations(list)
      if (list.length > 0 && refresh) {
        addAiLog('analysis', `分析完成: ${list.length} 场推荐（${sportTab === 'football' ? '足球' : '篮球'} · ${SITE_NAMES[siteTab] || siteTab}）`)
      }
      setRecsMeta({
        status: data.status || data.job_status || (list.length ? 'ready' : 'idle'),
        progress: Number(data.progress || 0),
        total: Number(data.total || 0),
        hint: data.hint || '',
        filterHint: data.filter_hint || '',
        minWinRate: data.min_win_rate ?? null,
        rawCount: Number(data.raw_count || 0),
        analysisEnabled: !!data.analysis_enabled,
      })
      // 金额默认取 AI 配置「单笔最大金额」（见输入框 value ?? formData.max_bet_amount）
      // 不把历史金额写入 state，避免出现偏离当前配置的旧金额
    } catch (err) {
      console.error('Load recommendations failed:', err)
      if (!silent) {
        toast.error(err?.detail || err?.message || '推荐加载失败，请稍后重试')
      }
    } finally {
      if (!silent) setRecsLoading(false)
    }
  }, [sportTab, siteTab])

  useEffect(() => {
    loadRecommendations(false, sportTab, siteTab)
  }, [sportTab, siteTab, loadRecommendations])

  // 切换人工/自动后按模式重新筛选展示（无需重跑 LLM）
  useEffect(() => {
    loadRecommendations(false, sportTab, siteTab, { silent: true })
  }, [betMode, engineStatus?.bet_mode]) // eslint-disable-line react-hooks/exhaustive-deps

  const handleStartAnalysis = async () => {
    setAnalysisBusy(true)
    try {
      await aiAPI.startAnalysis(sportTab, 80)
      setRecsMeta((m) => ({ ...m, analysisEnabled: true, status: 'starting' }))
      toast.success('分析已开始')
      addAiLog('analysis', `开始分析：${sportTab === 'football' ? '足球' : '篮球'} · ${SITE_NAMES[siteTab] || siteTab}`)
      await loadRecommendations(false, sportTab, siteTab, { silent: true })
    } catch (err) {
      toast.error(err?.detail || err?.message || '开始分析失败')
    } finally {
      setAnalysisBusy(false)
    }
  }

  const handleStopAnalysis = async () => {
    setAnalysisBusy(true)
    try {
      await aiAPI.stopAnalysis(sportTab)
      setRecsMeta((m) => ({ ...m, analysisEnabled: false, status: 'stopped' }))
      toast.success('分析已停止')
      addAiLog('analysis', '停止分析')
      await loadRecommendations(false, sportTab, siteTab, { silent: true })
    } catch (err) {
      toast.error(err?.detail || err?.message || '停止分析失败')
    } finally {
      setAnalysisBusy(false)
    }
  }

  // 后台分析开启时：短轮询拉取进度与 AI 小球推荐
  usePagePoll(
    () => loadRecommendations(false, sportTab, siteTab, { silent: true }),
    4000,
    { enabled: analysisOn },
  )

  const placeOneLeg = async ({ matchId, betType, selection, odds, stake, provider }) => {
    const bt = String(betType || 'total').toLowerCase()
    const allowed = {
      total: ['under'],
      moneyline: ['home', 'away', 'draw'],
      spread: ['home', 'away'],
    }
    if (!allowed[bt] || !allowed[bt].includes(selection)) {
      throw new Error('不支持的盘口或投注方向')
    }
    if (!odds || odds <= 1) throw new Error('赔率无效')
    const maxStake = Number(formData.max_bet_amount || 0)
    if (!stake || stake < minBetAmount) {
      throw new Error(`金额需 ≥${minBetAmount}（AI 配置）`)
    }
    if (maxStake > 0 && stake > maxStake) {
      throw new Error(`金额需 ≤${maxStake}（单笔上限）`)
    }
    return betsAPI.place({
      match_id: matchId,
      bet_type: bt,
      selection,
      stake,
      odds,
      provider: provider || siteTab || 'pinnacle',
    })
  }

  const handlePlaceFromRec = async (rec) => {
    // 走一键接口：服务端按 AI 配置门禁（止损止盈/每日笔数）
    const maxStake = Number(formData.max_bet_amount || 0)
    const stake = Number(stakeByMatch[rec.match_id] || maxStake || minBetAmount || 1)
    if (!stake || stake < minBetAmount) {
      toast.error(`金额需 ≥${minBetAmount}（AI 配置）`)
      return
    }
    if (maxStake > 0 && stake > maxStake) {
      toast.error(`金额需 ≤${maxStake}（单笔上限）`)
      return
    }
    setBettingId(`${rec.match_id}:primary`)
    try {
      const bt = String(rec?.recommendation?.bet_type || 'total').toLowerCase()
      const markets = ['moneyline', 'spread', 'total'].includes(bt) ? [bt] : ['total']
      const res = await aiAPI.oneClickBet(rec.match_id, stake, markets)
      const data = res.data || res
      const bet = (data.bets || [])[0]
      toast.success(
        bet
          ? `已投注 ${MARKET_LABEL[bet.market] || bet.market || ''} ${SEL_LABEL[bet.selection] || bet.selection} @ ${Number(bet.odds).toFixed(2)}`
          : (data.message || '已提交投注')
      )
      addAiLog('bet_placed', `一键投注: ${bet ? `${MARKET_LABEL[bet.market] || bet.market} ${SEL_LABEL[bet.selection] || bet.selection} @ ${Number(bet.odds).toFixed(2)}` : '已提交'}`, data)
      loadRecommendations(true)
    } catch (err) {
      toast.error(err?.detail || err?.message || '投注失败')
    } finally {
      setBettingId(null)
    }
  }

  // 一键投注：小球下注
  const handleOneClickAll = async (matchId) => {
    const stake = Number(stakeByMatch[matchId] || formData.max_bet_amount || minBetAmount || 1)
    setBettingId(`${matchId}:all`)
    try {
      const res = await aiAPI.oneClickBet(matchId, stake, [])
      const data = res.data || res
      toast.success(data.message || `已下注 ${data.bets?.length || 0} 笔`)
      loadRecommendations(true)
    } catch (err) {
      toast.error(err?.detail || err?.message || '一键投注失败')
    } finally {
      setBettingId(null)
    }
  }

  const handlePlaceCell = async (rec, market, cell) => {
    const bt = String(market?.bet_type || '').toLowerCase()
    if (!['total', 'moneyline', 'spread'].includes(bt)) {
      toast.error('不支持的盘口类型')
      return
    }
    if (!cell?.available || !cell.odds) {
      toast.error(cell?.disabled_reason || '该选项暂无可用赔率（未达配置区间）')
      return
    }
    const primarySel = rec?.recommendation?.selection
    const primaryBt = String(rec?.recommendation?.bet_type || '').toLowerCase()
    if (primaryBt && bt !== primaryBt) {
      toast.error('仅可投注全场小球盘口')
      return
    }
    if (primarySel && cell.selection !== primarySel) {
      toast.error('仅可投注主推方向（已按配置过滤）')
      return
    }
    // 与一键相同：AI 配置门禁
    await handlePlaceFromRec(rec)
  }

  const loadAll = async () => {
    setLoading(true)
    try {
      // 配置与推荐解耦，避免推荐超时导致配置加载失败
      const [statusRes, configRes] = await Promise.all([
        aiAPI.status(),
        aiAPI.config(),
      ])
      setEngineStatus(statusRes.data || {})
      setBetMode(statusRes.data?.bet_mode || 'manual')
      setConfig(configRes.data)

      const c = configRes.data || {}
      // 最低金额跟 AI 配置：默认 1，上限为单笔最大金额
      const minBet = Number(c.min_bet_amount ?? 1)
      setMinBetAmount(minBet > 0 ? minBet : 1)
      // 清空旧的动态金额（如 2.69），改回按配置单笔上限展示
      setStakeByMatch({})
      setFormData(normalizeFormData(c))
    } catch (err) {
      console.error('Load AI data failed:', err)
    } finally {
      setLoading(false)
    }
  }

  const handleStart = async () => {
    setStarting(true)
    try {
      const res = await aiAPI.start()
      toast.success(res.message || '自动下单已启动')
      addAiLog('engine', `${res.message || '自动下单已启动'}（${res.data?.effective_label || '运行中'}）`)
      setEngineStatus(res.data || { engine_running: true })
      setBetMode(res.data?.bet_mode || betMode)
      updateUser({ ai_enabled: true })
      loadRecommendations(true)
    } catch (err) {
      toast.error(err?.detail || err?.message || '启动失败')
    } finally {
      setStarting(false)
    }
  }

  const handleStop = async () => {
    try {
      const res = await aiAPI.stop()
      toast.success(res.message || '自动下单已停止')
      addAiLog('engine', res.message || '自动下单已停止')
      setEngineStatus(res.data || { engine_running: false, ai_enabled: false })
      updateUser({ ai_enabled: false })
    } catch (err) {
      toast.error(err?.detail || err?.message || '停止失败')
    }
  }

  const handleSaveConfig = async () => {
    try {
      await aiAPI.updateConfig(formData)
      toast.success('AI 设置已保存')
      setShowSettings(false)
      await loadAll()
      // 清旧推荐，按新配置重新分析
      await loadRecommendations(true)
    } catch (err) {
      toast.error('保存失败')
    }
  }

  const toggleCandidateSelection = (id) => {
    setSelectedCandidateIds((prev) => (
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
    ))
  }

  const toggleSelectAllCandidates = () => {
    if (selectedCandidateIds.length === visibleCandidates.length && visibleCandidates.length) {
      setSelectedCandidateIds([])
      return
    }
    setSelectedCandidateIds(visibleCandidates.map((item) => item.id))
  }

  const handleSelectHighScoreCandidates = () => {
    const picked = visibleCandidates
      .filter((item) => Number(item.best_score || 0) >= Number(highScoreThreshold || 0))
      .map((item) => item.id)
    setSelectedCandidateIds(picked)
    if (!picked.length) {
      toast.error('当前筛选下没有达到阈值的候选别名')
    }
  }

  const toggleOverrideSelection = (id) => {
    setSelectedOverrideIds((prev) => (
      prev.includes(id) ? prev.filter((item) => item !== id) : [...prev, id]
    ))
  }

  const toggleSelectAllOverrides = () => {
    if (selectedVisibleOverrideCount === visibleOverrides.length && visibleOverrides.length) {
      setSelectedOverrideIds([])
      return
    }
    setSelectedOverrideIds(visibleOverrides.map((item) => item.id))
  }

  const handleApproveSelectedCandidates = async () => {
    if (!selectedCandidateIds.length) {
      toast.error('请先勾选候选别名')
      return
    }
    setAliasLoading(true)
    try {
      const res = await adminAPI.approveAliasCandidatesBatch(selectedCandidateIds, true)
      const approvedCount = Number(res.data?.approved_count || 0)
      toast.success(`已批准 ${approvedCount} 条候选别名`)
      setSelectedCandidateIds([])
      await loadAliasData({ silent: true })
    } catch (e) {
      toast.error(e?.message || '批量批准失败')
    } finally {
      setAliasLoading(false)
    }
  }

  const handleApproveSingleCandidate = async (id) => {
    setAliasLoading(true)
    try {
      await adminAPI.approveAliasCandidate(id, true)
      toast.success('候选别名已生效')
      await loadAliasData({ silent: true })
    } catch (e) {
      toast.error(e?.message || '批准失败')
    } finally {
      setAliasLoading(false)
    }
  }

  const handleExportOverrides = async () => {
    try {
      const res = await adminAPI.exportAliasOverrides('all', 5000)
      const data = res.data || {}
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:T]/g, '-')
      downloadJson(`nowscore-alias-overrides-${stamp}.json`, data)
      toast.success(`已导出 ${data.count || 0} 条正式别名`)
    } catch (e) {
      toast.error(e?.message || '导出失败')
    }
  }

  const handlePreviewImportOverrides = async () => {
    const { items, error } = parseAliasImportDraft(importText)
    if (error) {
      toast.error(error)
      return
    }
    if (!items.length) {
      toast.error('导入内容里没有可用的别名 items')
      return
    }
    setPreviewLoading(true)
    try {
      const res = await adminAPI.previewAliasOverridesImport(items)
      setImportPreview(res.data || null)
      const summary = res.data?.summary || {}
      toast.success(
        `预览完成：新增 ${summary.create || 0}，更新 ${summary.update || 0}，不变 ${summary.nochange || 0}`,
      )
    } catch (e) {
      toast.error(e?.message || '导入预览失败')
    } finally {
      setPreviewLoading(false)
    }
  }

  const handleImportOverrides = async () => {
    const { items, error } = parseAliasImportDraft(importText)
    if (error) {
      toast.error(error)
      return
    }
    if (!items.length) {
      toast.error('导入内容里没有可用的别名 items')
      return
    }
    setImportLoading(true)
    try {
      const res = await adminAPI.importAliasOverrides(items)
      const saved = Number(res.data?.saved_count || 0)
      const skipped = Number(res.data?.skipped_count || 0)
      toast.success(`导入完成：成功 ${saved} 条，跳过 ${skipped} 条`)
      setImportText('')
      setImportPreview(null)
      await loadAliasData({ silent: true })
    } catch (e) {
      toast.error(e?.message || '导入失败')
    } finally {
      setImportLoading(false)
    }
  }

  const handleImportFile = async (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return
    try {
      const text = await file.text()
      setImportText(text)
      toast.success('导入文件已加载，请确认后执行导入')
    } catch {
      toast.error('读取文件失败')
    }
  }

  const handleDeleteOverride = async (recordId) => {
    setAliasLoading(true)
    try {
      await adminAPI.deleteAliasOverride(recordId)
      toast.success('正式别名已删除')
      await loadAliasData({ silent: true })
    } catch (e) {
      toast.error(e?.message || '删除失败')
    } finally {
      setAliasLoading(false)
    }
  }

  const handleDeleteSelectedOverrides = async () => {
    if (!selectedOverrideIds.length) {
      toast.error('请先勾选正式别名')
      return
    }
    setAliasLoading(true)
    try {
      const res = await adminAPI.deleteAliasOverridesBatch(selectedOverrideIds)
      const deletedCount = Number(res.data?.deleted_count || 0)
      const missedCount = Number(res.data?.missed_count || 0)
      toast.success(`已删除 ${deletedCount} 条正式别名${missedCount ? `，未命中 ${missedCount} 条` : ''}`)
      setSelectedOverrideIds([])
      await loadAliasData({ silent: true })
    } catch (e) {
      toast.error(e?.message || '批量删除失败')
    } finally {
      setAliasLoading(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 size={32} className="animate-spin text-brand-500" />
      </div>
    )
  }

  const engineRunning = !!(engineStatus?.engine_running ?? engineStatus?.running)
  const isAuto = (engineStatus?.execution_mode || engineStatus?.bet_mode || betMode) === 'active'
  const manualAnalysisRunning = !!(engineStatus?.manual_analysis_running || analysisOn)
  const badgeTone = engineStatus?.badge_tone || 'slate'
  const aiSwitchOn = Boolean(config?.is_active ?? formData.is_active)
  const statusTitle = !aiSwitchOn
    ? 'AI 已关闭'
    : (engineStatus?.effective_label || (
      engineRunning
        ? (isAuto ? '自动运行中' : 'AI 运行中')
        : (manualAnalysisRunning ? '手动分析中' : 'AI 未启动')
    ))
  const statusDesc = !aiSwitchOn
    ? '请先在 AI 设置中开启 AI。'
    : (engineStatus?.effective_description || (
      engineRunning
        ? (isAuto ? '系统正在分析并自动下单。' : '系统正在运行。')
        : (manualAnalysisRunning ? '系统正在分析，只出推荐，不会自动下单。' : '当前不会分析，也不会下单。')
    ))
  const runtimeLimits = engineStatus?.runtime_limits || config?.runtime_limits || {}
  const scanIntervalSec = Number(runtimeLimits.scan_interval_sec || config?.runtime_limits?.scan_interval_sec || 120)
  const scanSummary = isAuto
    ? `每 ${formatIntervalLabel(scanIntervalSec)} 扫描一次 · 流式下单`
    : `每 ${formatIntervalLabel(scanIntervalSec)} 扫描一次 · 手动分析`
  const dsLast = dsMeta?.last_result
  const dsLastText = dsLast?.finished_at
    ? new Date(Number(dsLast.finished_at) * 1000).toLocaleString('zh-CN', { hour12: false })
    : ''
  const candidateQuery = candidateSearch.trim().toLowerCase()
  const overrideQuery = overrideSearch.trim().toLowerCase()
  const minCandidateScore = minScoreFilter === '' ? null : Number(minScoreFilter)
  const maxCandidateScore = maxScoreFilter === '' ? null : Number(maxScoreFilter)
  const visibleCandidates = [...aliasCandidates]
    .filter((item) => aliasSportFilter === 'all' || item.sport === aliasSportFilter)
    .filter((item) => {
      const score = Number(item.best_score || 0)
      if (minCandidateScore != null && Number.isFinite(minCandidateScore) && score < minCandidateScore) return false
      if (maxCandidateScore != null && Number.isFinite(maxCandidateScore) && score > maxCandidateScore) return false
      return true
    })
    .filter((item) => {
      if (!candidateQuery) return true
      const text = [
        item.source_home,
        item.source_away,
        item.candidate_home,
        item.candidate_away,
        item.candidate_title,
      ].join(' ').toLowerCase()
      return text.includes(candidateQuery)
    })
    .sort((a, b) => {
      if (candidateSort === 'count_desc') return Number(b.count || 0) - Number(a.count || 0)
      if (candidateSort === 'recent_desc') {
        return new Date(b.last_seen_at || 0).getTime() - new Date(a.last_seen_at || 0).getTime()
      }
      return Number(b.best_score || 0) - Number(a.best_score || 0)
    })
  const visibleOverrides = [...aliasOverrides]
    .filter((item) => aliasSportFilter === 'all' || item.sport === aliasSportFilter)
    .filter((item) => {
      if (!overrideQuery) return true
      const text = [
        item.source_home,
        item.source_away,
        item.candidate_home,
        item.candidate_away,
        ...(item.home_alias_group || []),
        ...(item.away_alias_group || []),
      ].join(' ').toLowerCase()
      return text.includes(overrideQuery)
    })
    .sort((a, b) => {
      if (overrideSort === 'score_desc') return Number(b.best_score || 0) - Number(a.best_score || 0)
      if (overrideSort === 'name_asc') {
        return `${a.source_home || ''}${a.source_away || ''}`.localeCompare(`${b.source_home || ''}${b.source_away || ''}`, 'zh-CN')
      }
      return new Date(b.approved_at || 0).getTime() - new Date(a.approved_at || 0).getTime()
    })
  const selectedVisibleCount = visibleCandidates.filter((item) => selectedCandidateIds.includes(item.id)).length
  const selectedVisibleOverrideCount = visibleOverrides.filter((item) => selectedOverrideIds.includes(item.id)).length
  const highScoreVisibleCount = visibleCandidates.filter((item) => Number(item.best_score || 0) >= Number(highScoreThreshold || 0)).length
  const { items: importDraftItems, error: importDraftError } = parseAliasImportDraft(importText)
  const importDraftCount = importDraftItems.length
  const importPreviewSummary = importPreview?.summary || {}
  const importPreviewItems = Array.isArray(importPreview?.items) ? importPreview.items.slice(0, 8) : []

  return (
    <div className="page">
      <PageHeader
        eyebrow="智能"
        title="AI 投注"
        description={`OB / 平博滚球 · 全场小球 · ${scanSummary}`}
        actions={(
          <>
            <BetModeSwitch
              onChange={async (data) => {
                if (!data) return
                setBetMode(data.bet_mode || 'manual')
                try {
                  const statusRes = await aiAPI.status()
                  setEngineStatus(statusRes.data || {})
                } catch {
                  setEngineStatus((s) => ({ ...(s || {}), ...data }))
                }
                // 模式切换后立即按新规则刷列表
                loadRecommendations(false, sportTab, siteTab, { silent: true })
              }}
            />
            {isAuto ? (
              !engineRunning ? (
                <button
                  onClick={handleStart}
                  disabled={starting || !engineStatus || !aiSwitchOn || manualAnalysisRunning}
                  className="btn-success flex items-center gap-2 px-6"
                >
                  {starting || !engineStatus ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
                  {engineStatus ? '启动自动下单' : '加载中…'}
                </button>
              ) : (
                <button
                  onClick={handleStop}
                  className="btn-danger flex items-center gap-2 px-6"
                >
                  <Square size={16} /> 停止自动下单
                </button>
              )
            ) : (
              <span className="text-xs text-ink-500 px-2">
                人工模式使用下方“开始分析”
              </span>
            )}
            <button
              onClick={() => setShowSettings(!showSettings)}
              className="btn-outline flex items-center gap-2"
            >
              <Settings size={16} /> 配置
            </button>
          </>
        )}
      />

      {/* 数据源开关 */}
      <div className="card mb-4">
        <div className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className={`w-10 h-10 rounded-xl flex items-center justify-center ${
              dsEnabled ? 'bg-brand-50 text-brand-700' : 'bg-ink-100 text-ink-400'
            }`}>
              <Database size={18} />
            </div>
            <div>
              <div className="font-semibold text-ink-900 text-sm">捷报数据</div>
              <div className="text-xs text-ink-500">
                {dsEnabled === null ? '加载中…' : dsEnabled
                  ? '已开启 · 每小时自动更新'
                  : '已关闭 · 仅使用当前缓存'}
              </div>
              {dsEnabled && dsLastText ? (
                <div className="text-[11px] text-ink-400 mt-1">
                  {dsLast?.ok === false
                    ? `上次失败 · ${dsLastText} · ${dsLast.error || '未知错误'}`
                    : `上次完成 · ${dsLastText} · 足球 ${dsLast?.football_cached || 0} 场 / 篮球 ${dsLast?.basketball_cached || 0} 场`}
                </div>
              ) : null}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={toggleDs}
              disabled={dsLoading || dsEnabled === null}
              className={`flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors ${
                dsEnabled
                  ? 'bg-brand-700 text-white hover:bg-brand-800'
                  : 'bg-ink-200 text-ink-600 hover:bg-ink-300'
              }`}
            >
              {dsLoading ? <Loader2 size={14} className="animate-spin" /> : <Power size={14} />}
              {dsEnabled === null ? '加载中…' : dsEnabled ? '开启' : '关闭'}
            </button>
          </div>
        </div>

        {/* 进度条 */}
        {prefetchProgress && prefetchProgress.total > 0 && (
          <div className="mt-3">
            <div className="flex items-center justify-between text-xs text-ink-500 mb-1">
              <span>{prefetchProgress.sport} · {prefetchProgress.done}/{prefetchProgress.total}</span>
              <span>{prefetchProgress.elapsed != null ? `${Math.round(prefetchProgress.elapsed)}s` : ''}</span>
            </div>
            <div className="w-full h-2 bg-ink-100 rounded-full overflow-hidden">
              <div
                className="h-full bg-brand-600 rounded-full transition-all duration-300"
                style={{ width: `${Math.min(100, (prefetchProgress.done / prefetchProgress.total) * 100)}%` }}
              />
            </div>
          </div>
        )}
      </div>

      <div className="card mb-6">
        <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
          <div>
            <div className="font-semibold text-ink-900 text-sm">别名管理</div>
            <div className="text-xs text-ink-500 mt-1">
              处理未命中队名，支持批量批准、导出和导入，批准后立即生效。
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <div className="flex flex-wrap items-center gap-1 rounded-xl bg-ink-50 p-1">
              {ALIAS_SPORT_TABS.map((tab) => (
                <button
                  key={tab.key}
                  type="button"
                  onClick={() => setAliasSportFilter(tab.key)}
                  className={`px-2.5 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                    aliasSportFilter === tab.key
                      ? 'bg-white text-brand-700 shadow-sm'
                      : 'text-ink-500 hover:text-ink-700'
                  }`}
                >
                  {tab.label}
                </button>
              ))}
            </div>
            <button
              onClick={() => loadAliasData()}
              disabled={aliasLoading}
              className="btn-outline flex items-center gap-2"
            >
              {aliasLoading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
              刷新
            </button>
            <button
              onClick={handleApproveSelectedCandidates}
              disabled={aliasLoading || !selectedCandidateIds.length}
              className="btn-outline flex items-center gap-2"
            >
              <CheckSquare size={14} />
              批量批准
            </button>
            <button
              onClick={handleExportOverrides}
              className="btn-outline flex items-center gap-2"
            >
              <Download size={14} />
              导出正式别名
            </button>
            <button
              onClick={handleDeleteSelectedOverrides}
              disabled={aliasLoading || !selectedOverrideIds.length}
              className="btn-outline flex items-center gap-2"
            >
              <Trash2 size={14} />
              批量删除正式别名
            </button>
          </div>
        </div>

        <div className="grid xl:grid-cols-2 gap-4 mt-4">
          <div className="rounded-2xl border border-ink-100 p-4">
            <div className="flex items-center justify-between gap-3 mb-3">
              <div>
                <div className="font-medium text-sm text-ink-900">候选别名</div>
                <div className="text-xs text-ink-500">
                  当前显示 {visibleCandidates.length} 条，已勾选 {selectedVisibleCount} 条
                </div>
              </div>
              <button
                onClick={toggleSelectAllCandidates}
                disabled={!visibleCandidates.length}
                className="text-xs text-brand-700 hover:text-brand-800"
              >
                {selectedVisibleCount === visibleCandidates.length && visibleCandidates.length ? '取消全选' : '全选'}
              </button>
            </div>

            <div className="grid md:grid-cols-[1fr_auto_auto_auto] gap-2 mb-3">
              <input
                value={candidateSearch}
                onChange={(e) => setCandidateSearch(e.target.value)}
                className="input"
                placeholder="搜索原始队名 / 推荐队名"
              />
              <select
                value={candidateSort}
                onChange={(e) => setCandidateSort(e.target.value)}
                className="input min-w-[10rem]"
              >
                {CANDIDATE_SORTS.map((item) => (
                  <option key={item.key} value={item.key}>{item.label}</option>
                ))}
              </select>
              <input
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={minScoreFilter}
                onChange={(e) => setMinScoreFilter(e.target.value)}
                className="input min-w-[7rem]"
                placeholder="最低分"
              />
              <input
                type="number"
                min="0"
                max="1"
                step="0.01"
                value={maxScoreFilter}
                onChange={(e) => setMaxScoreFilter(e.target.value)}
                className="input min-w-[7rem]"
                placeholder="最高分"
              />
            </div>

            <div className="flex flex-wrap items-center gap-2 mb-3 text-xs">
              <div className="flex items-center gap-2 rounded-xl border border-ink-100 px-3 py-2 bg-ink-50">
                <span className="text-xs text-ink-500">高分阈值</span>
                <input
                  type="number"
                  min="0.3"
                  max="0.99"
                  step="0.01"
                  value={highScoreThreshold}
                  onChange={(e) => setHighScoreThreshold(Number(e.target.value) || 0.6)}
                  className="w-16 bg-transparent text-sm text-ink-900 outline-none"
                />
              </div>
              {(minCandidateScore != null || maxCandidateScore != null) ? (
                <span className="rounded-full bg-amber-50 px-2.5 py-1 text-amber-700">
                  分数区间 {minCandidateScore ?? 0} - {maxCandidateScore ?? 1}
                </span>
              ) : null}
              <span className="rounded-full bg-ink-50 px-2.5 py-1 text-ink-500">
                高分候选 {highScoreVisibleCount} 条
              </span>
              <button
                onClick={handleSelectHighScoreCandidates}
                disabled={!visibleCandidates.length}
                className="text-brand-700 hover:text-brand-800 font-medium"
              >
                一键全选高分候选
              </button>
            </div>

            <div className="space-y-3 max-h-[28rem] overflow-auto pr-1">
              {visibleCandidates.length === 0 ? (
                <div className="rounded-xl bg-ink-50 px-3 py-4 text-sm text-ink-500">
                  当前筛选下暂无候选别名。
                </div>
              ) : visibleCandidates.map((item) => (
                <label
                  key={item.id}
                  className="block rounded-xl border border-ink-100 bg-white px-3 py-3"
                >
                  <div className="flex items-start gap-3">
                    <input
                      type="checkbox"
                      checked={selectedCandidateIds.includes(item.id)}
                      onChange={() => toggleCandidateSelection(item.id)}
                      className="mt-1 accent-brand-500"
                    />
                    <div className="min-w-0 flex-1">
                      <div className="text-sm font-medium text-ink-900 break-words">
                        {item.source_home} vs {item.source_away}
                      </div>
                      <div className="text-xs text-ink-500 mt-1 break-words">
                        推荐映射：{item.candidate_home} vs {item.candidate_away}
                      </div>
                      <div className="text-xs text-ink-400 mt-1">
                        分数 {Number(item.best_score || 0).toFixed(3)} · 出现 {item.count || 0} 次
                      </div>
                      <div className="text-[11px] text-ink-400 mt-1">
                        最近出现：{formatDateTime(item.last_seen_at)}
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => handleApproveSingleCandidate(item.id)}
                      disabled={aliasLoading}
                      className="btn-outline text-xs px-2.5 py-1.5 shrink-0"
                    >
                      批准
                    </button>
                  </div>
                </label>
              ))}
            </div>
          </div>

          <div className="rounded-2xl border border-ink-100 p-4">
            <div className="flex items-center justify-between gap-3 mb-3">
              <div>
                <div className="font-medium text-sm text-ink-900">正式别名</div>
                <div className="text-xs text-ink-500">
                  当前显示 {visibleOverrides.length} 条，已勾选 {selectedVisibleOverrideCount} 条
                </div>
              </div>
              <button
                onClick={toggleSelectAllOverrides}
                disabled={!visibleOverrides.length}
                className="text-xs text-brand-700 hover:text-brand-800"
              >
                {selectedVisibleOverrideCount === visibleOverrides.length && visibleOverrides.length ? '取消全选' : '全选'}
              </button>
            </div>

            <div className="grid md:grid-cols-[1fr_auto] gap-2 mb-3">
              <input
                value={overrideSearch}
                onChange={(e) => setOverrideSearch(e.target.value)}
                className="input"
                placeholder="搜索正式别名"
              />
              <select
                value={overrideSort}
                onChange={(e) => setOverrideSort(e.target.value)}
                className="input min-w-[10rem]"
              >
                {OVERRIDE_SORTS.map((item) => (
                  <option key={item.key} value={item.key}>{item.label}</option>
                ))}
              </select>
            </div>

            <div className="space-y-3 max-h-[18rem] overflow-auto pr-1">
              {visibleOverrides.length === 0 ? (
                <div className="rounded-xl bg-ink-50 px-3 py-4 text-sm text-ink-500">
                  当前筛选下暂无正式别名。
                </div>
              ) : visibleOverrides.map((item) => (
                <label
                  key={item.id}
                  className="block rounded-xl border border-ink-100 bg-white px-3 py-3"
                >
                  <div className="flex items-start justify-between gap-3">
                    <div className="flex items-start gap-3 min-w-0 flex-1">
                      <input
                        type="checkbox"
                        checked={selectedOverrideIds.includes(item.id)}
                        onChange={() => toggleOverrideSelection(item.id)}
                        className="mt-1 accent-brand-500"
                      />
                      <div className="min-w-0">
                        <div className="text-sm font-medium text-ink-900 break-words">
                          {item.source_home} vs {item.source_away}
                        </div>
                        <div className="text-xs text-ink-500 mt-1 break-words">
                          生效映射：{(item.home_alias_group || []).join(' / ')} ｜ {(item.away_alias_group || []).join(' / ')}
                        </div>
                        <div className="text-[11px] text-ink-400 mt-1">
                          分数 {Number(item.best_score || 0).toFixed(3)} · 批准人：{item.approved_by || '-'} · {formatDateTime(item.approved_at)}
                        </div>
                      </div>
                    </div>
                    <button
                      type="button"
                      onClick={() => handleDeleteOverride(item.id)}
                      disabled={aliasLoading}
                      className="btn-outline text-xs px-2.5 py-1.5 shrink-0"
                    >
                      <Trash2 size={12} />
                    </button>
                  </div>
                </label>
              ))}
            </div>

            <div className="mt-4 pt-4 border-t border-ink-100">
              <div className="flex items-center justify-between gap-3 mb-2">
                <div className="text-sm font-medium text-ink-900">导入正式别名</div>
                <label className="btn-outline flex items-center gap-2 cursor-pointer">
                  <Upload size={14} />
                  选择文件
                  <input
                    type="file"
                    accept="application/json,.json"
                    onChange={handleImportFile}
                    className="hidden"
                  />
                </label>
              </div>
              <textarea
                value={importText}
                onChange={(e) => {
                  setImportText(e.target.value)
                  setImportPreview(null)
                }}
                className="input min-h-[10rem] font-mono text-xs"
                placeholder='粘贴导出的 JSON，或先选择 .json 文件'
              />
              <div className="mt-2 text-xs">
                {importText.trim() ? (
                  importDraftError ? (
                    <span className="text-rose-600">{importDraftError}</span>
                  ) : (
                    <span className="text-brand-700">检测到 {importDraftCount} 条可导入别名</span>
                  )
                ) : (
                  <span className="text-ink-400">支持直接粘贴导出的 JSON，或先选择文件再导入。</span>
                )}
              </div>
              {importPreview ? (
                <div className="mt-3 rounded-xl border border-ink-100 bg-ink-50 p-3">
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    <span className="rounded-full bg-emerald-50 px-2.5 py-1 text-emerald-700">新增 {importPreviewSummary.create || 0}</span>
                    <span className="rounded-full bg-amber-50 px-2.5 py-1 text-amber-700">更新 {importPreviewSummary.update || 0}</span>
                    <span className="rounded-full bg-slate-100 px-2.5 py-1 text-slate-700">不变 {importPreviewSummary.nochange || 0}</span>
                    <span className="rounded-full bg-rose-50 px-2.5 py-1 text-rose-700">无效 {importPreviewSummary.invalid || 0}</span>
                  </div>
                  <div className="mt-3 space-y-2 max-h-48 overflow-auto pr-1">
                    {importPreviewItems.map((item) => (
                      <div key={`${item.index}-${item.id || item.action}`} className="rounded-lg bg-white px-3 py-2 text-xs text-ink-600">
                        <div className="font-medium text-ink-900">
                          {item.action === 'create' ? '新增' : item.action === 'update' ? '更新' : item.action === 'nochange' ? '不变' : '无效'}
                          {' · '}
                          {item.source_home || item.item?.source_home || '-'} vs {item.source_away || item.item?.source_away || '-'}
                        </div>
                        {item.after ? (
                          <div className="mt-1 text-[11px] text-ink-500 break-words">
                            目标映射：{item.after.candidate_home} vs {item.after.candidate_away}
                          </div>
                        ) : null}
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}
              <div className="flex items-center justify-end gap-2 mt-3">
                <button
                  onClick={() => {
                    setImportText('')
                    setImportPreview(null)
                  }}
                  disabled={!importText}
                  className="btn-outline"
                >
                  清空
                </button>
                <button
                  onClick={handlePreviewImportOverrides}
                  disabled={previewLoading || !importText.trim() || !!importDraftError || importDraftCount <= 0}
                  className="btn-outline flex items-center gap-2"
                >
                  {previewLoading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />}
                  预览差异
                </button>
                <button
                  onClick={handleImportOverrides}
                  disabled={importLoading || !importText.trim() || !!importDraftError || importDraftCount <= 0}
                  className="btn-success flex items-center gap-2"
                >
                  {importLoading ? <Loader2 size={14} className="animate-spin" /> : <Upload size={14} />}
                  导入并生效
                </button>
              </div>
            </div>
          </div>
        </div>

        <div className="rounded-2xl border border-ink-100 p-4 mt-4">
          <div className="flex items-center justify-between gap-3 mb-3">
            <div>
              <div className="font-medium text-sm text-ink-900">操作日志</div>
              <div className="text-xs text-ink-500">
                最近的批准、删除、导入动作都会记录在这里。
              </div>
            </div>
            <span className="text-xs text-ink-400">最近 {aliasAuditLogs.length} 条</span>
          </div>
          <div className="space-y-2 max-h-60 overflow-auto pr-1">
            {aliasAuditLogs.length === 0 ? (
              <div className="rounded-xl bg-ink-50 px-3 py-4 text-sm text-ink-500">
                暂无操作日志。
              </div>
            ) : aliasAuditLogs.map((item, index) => (
              <div key={`${item.time || 'time'}-${index}`} className="rounded-xl border border-ink-100 bg-white px-3 py-3">
                <div className="flex flex-wrap items-center gap-2 text-xs">
                  <span className="rounded-full bg-ink-100 px-2.5 py-1 text-ink-700">{item.action || 'unknown'}</span>
                  <span className="text-ink-500">{item.actor || 'system'}</span>
                  <span className="text-ink-400">{formatDateTime(item.time)}</span>
                </div>
                <div className="mt-2 text-xs text-ink-600 break-words">
                  {item.payload?.source_home || item.payload?.source_away
                    ? `${item.payload?.source_home || '-'} vs ${item.payload?.source_away || '-'}`
                    : '系统级操作'}
                </div>
                <div className="mt-1 text-[11px] text-ink-400 break-all">
                  {JSON.stringify(item.payload || {})}
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* 状态横幅 */}
      <div className={`card mb-6 border ${
        badgeTone === 'green'
          ? 'border-green-500/30 bg-green-500/5'
          : badgeTone === 'amber'
            ? 'border-yellow-500/30 bg-yellow-500/5'
            : badgeTone === 'orange'
              ? 'border-orange-500/30 bg-orange-500/5'
              : 'border-gray-500/30'
      }`}>
        <div className="flex items-center gap-3">
          <div className={`w-3 h-3 rounded-full ${
            badgeTone === 'green'
              ? 'bg-green-400 animate-pulse'
              : badgeTone === 'amber'
                ? 'bg-yellow-400 animate-pulse'
                : badgeTone === 'orange'
                  ? 'bg-orange-400 animate-pulse'
                  : 'bg-gray-500'
          }`} />
          <span className="font-medium">
            {engineStatus === null ? '正在加载…' : statusTitle}
          </span>
          {(engineRunning || manualAnalysisRunning) && (
            <span className="text-xs text-ink-500 ml-auto">
              {scanSummary}
            </span>
          )}
        </div>
        <p className="text-xs text-ink-500 mt-2 pl-6">{statusDesc}</p>
        {recsMeta.filterHint ? (
          <p className="text-xs text-ink-500 mt-2 pl-6">{recsMeta.filterHint}</p>
        ) : null}
      </div>

      {/* 配置面板 */}
      {showSettings && (
        <div className="card mb-6">
          <h3 className="font-bold mb-4">AI 设置</h3>

          <div className="grid md:grid-cols-2 gap-4">
            {/* AI 开关 */}
            <div>
              <label className="flex items-center gap-2 text-sm text-ink-400">
                <input
                  type="checkbox"
                  checked={!!formData.is_active}
                  onChange={(e) => setFormData({ ...formData, is_active: e.target.checked })}
                  className="accent-brand-500"
                />
                AI 开关
              </label>
              <p className="text-xs text-ink-600 mt-1">关闭后不能开始分析或自动下单</p>
            </div>

            {/* 单笔最大金额 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">单笔最大金额</label>
              <input
                type="number"
                value={formData.max_bet_amount}
                onChange={(e) => setFormData({ ...formData, max_bet_amount: Number(e.target.value) })}
                className="input"
                min={minBetAmount}
              />
            </div>

            {/* 每日最多笔数 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">每日最多投注数</label>
              <input
                type="number"
                value={formData.max_daily_bets}
                onChange={(e) => setFormData({ ...formData, max_daily_bets: Number(e.target.value) })}
                className="input"
                min="1"
                max="100"
              />
            </div>

            {/* 止损 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">日止损线</label>
              <input
                type="number"
                value={formData.stop_loss}
                onChange={(e) => setFormData({ ...formData, stop_loss: Number(e.target.value) })}
                className="input"
                min="10"
              />
            </div>

            {/* 止盈 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">日止盈线</label>
              <input
                type="number"
                value={formData.take_profit}
                onChange={(e) => setFormData({ ...formData, take_profit: Number(e.target.value) })}
                className="input"
                min="10"
              />
            </div>

            {/* 最低置信度 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">最低置信度</label>
              <input
                type="number"
                value={Math.round((formData.min_confidence || 0) * 100)}
                onChange={(e) => setFormData({ ...formData, min_confidence: Number(e.target.value) / 100 })}
                className="input"
                min="10"
                max="99"
                step="1"
              />
              <p className="text-xs text-ink-600 mt-1">低于此值的推荐会被过滤（当前 {Math.round((formData.min_confidence || 0) * 100)}%）</p>
            </div>

            {/* 最低赔率 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">最低赔率</label>
              <input
                type="number"
                value={formData.min_odds}
                onChange={(e) => setFormData({ ...formData, min_odds: Number(e.target.value) })}
                className="input"
                min="1.01"
                step="0.01"
              />
              <p className="text-xs text-ink-600 mt-1">赔率低于此值的推荐将被过滤</p>
            </div>

            {/* 最高赔率 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">最高赔率</label>
              <input
                type="number"
                value={formData.max_odds}
                onChange={(e) => setFormData({ ...formData, max_odds: Number(e.target.value) })}
                className="input"
                min="1.02"
                step="0.01"
              />
              <p className="text-xs text-ink-600 mt-1">赔率高于此值的推荐将被过滤</p>
            </div>

            {/* 偏好球类 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">偏好球类</label>
              <div className="flex flex-wrap gap-3">
                {SPORT_TABS.map((tab) => (
                  <label key={tab.key} className="flex items-center gap-1.5 text-sm text-ink-600">
                    <input
                      type="checkbox"
                      checked={formData.preferred_sports.includes(tab.key)}
                      onChange={(e) => setFormData((prev) => {
                        const set = new Set(prev.preferred_sports)
                        if (e.target.checked) set.add(tab.key)
                        else set.delete(tab.key)
                        return { ...prev, preferred_sports: Array.from(set) }
                      })}
                      className="accent-brand-500"
                    />
                    {tab.label}
                  </label>
                ))}
              </div>
            </div>

            {/* 排除球队 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">排除球队</label>
              <input
                type="text"
                value={formData.excluded_teams.join('，')}
                onChange={(e) => setFormData({ ...formData, excluded_teams: e.target.value.split(/[，,]/).map((s) => s.trim()).filter(Boolean) })}
                className="input"
                placeholder="多个球队用逗号分隔"
              />
            </div>
          </div>

          <div className="flex justify-end gap-3 mt-6">
            <button onClick={() => setShowSettings(false)} className="btn-outline">
              取消
            </button>
            <button onClick={handleSaveConfig} className="btn-primary">
              保存配置
            </button>
          </div>
        </div>
      )}

      {/* AI推荐列表 */}
      <div className="card">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <h3 className="font-bold flex items-center gap-2">
            <Brain size={18} className="text-brand-700" />
            AI 推荐
          </h3>
          <div className="flex flex-wrap items-center gap-2">
            {!isAuto ? (
              !analysisOn ? (
                <button
                  type="button"
                  onClick={handleStartAnalysis}
                  disabled={analysisBusy || !aiSwitchOn || engineRunning}
                  className="btn-success flex items-center gap-1.5 px-3 py-1.5 text-sm"
                >
                  {analysisBusy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                  开始分析
                </button>
              ) : (
                <button
                  type="button"
                  onClick={handleStopAnalysis}
                  disabled={analysisBusy}
                  className="btn-danger flex items-center gap-1.5 px-3 py-1.5 text-sm"
                >
                  {analysisBusy ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} />}
                  停止分析
                </button>
              )
            ) : (
              <span className="text-xs text-ink-500 px-2">
                自动模式下不启用手动分析开关
              </span>
            )}
            <button
              onClick={() => loadRecommendations(false, sportTab, siteTab)}
              disabled={recsLoading}
              className="text-sm text-brand-700 hover:text-brand-300 disabled:opacity-50 px-2"
            >
              {recsLoading ? '加载中…' : '刷新'}
            </button>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2 mb-3">
          {SITE_TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              onClick={() => setSiteTab(tab.key)}
              className={`px-3 py-1.5 rounded-full text-sm border transition-colors ${
                siteTab === tab.key
                  ? 'bg-ink-800 text-white border-ink-800'
                  : 'bg-white text-ink-600 border-ink-200 hover:border-ink-400'
              }`}
            >
              {tab.label}
            </button>
          ))}
          <span className="mx-1 h-5 w-px bg-ink-200 hidden sm:inline-block" />
          {SPORT_TABS.map((tab) => (
            <button
              key={tab.key}
              type="button"
              onClick={() => setSportTab(tab.key)}
              className={`px-3 py-1.5 rounded-full text-sm border transition-colors ${
                sportTab === tab.key
                  ? 'bg-brand-600 text-white border-brand-600'
                  : 'bg-white text-ink-600 border-ink-200 hover:border-brand-300'
              }`}
            >
              {tab.label}
            </button>
          ))}
          <span className="text-xs text-ink-400 ml-1">滚球 · 全场小球</span>
        </div>

        {!isAuto && analysisOn && (
          <div className="mb-3 flex items-center gap-2 text-sm text-ink-600 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2">
            {analyzing ? (
              <Loader2 size={16} className="animate-spin text-brand-600 shrink-0" />
            ) : (
              <span className="w-2.5 h-2.5 rounded-full bg-brand-500 animate-pulse shrink-0" />
            )}
            <span>
              {analyzing ? '正在分析滚球，结果会自动刷新' : '分析已开启，结果会自动刷新'}
              {SPORT_TABS.find((t) => t.key === sportTab)?.label || ''}
              {SITE_NAMES[siteTab] ? ` · ${SITE_NAMES[siteTab]}` : ''}
              {recsMeta.total > 0 ? `（${recsMeta.progress}/${recsMeta.total}）` : ''}
            </span>
          </div>
        )}

        {recsLoading && recommendations.length === 0 && !analyzing ? (
          <div className="text-center py-10 text-ink-500">
            <Loader2 size={32} className="mx-auto mb-3 animate-spin text-brand-500" />
            <p>正在加载{SPORT_TABS.find((t) => t.key === sportTab)?.label}推荐…</p>
          </div>
        ) : recommendations.length === 0 ? (
          <div className="text-center py-10 text-ink-500">
            {analyzing ? (
              <>
                <Loader2 size={40} className="mx-auto mb-3 animate-spin text-brand-500" />
                <p>正在分析滚球并生成推荐</p>
                <p className="text-xs mt-1">
                  {recsMeta.total > 0
                      ? `进度 ${recsMeta.progress}/${recsMeta.total}，完成后自动显示`
                    : '结果就绪后会自动显示，无需反复刷新本页'}
                </p>
              </>
            ) : (
              <>
                <Bot size={48} className="mx-auto mb-3 opacity-30" />
                <p>暂无{SPORT_TABS.find((t) => t.key === sportTab)?.label}推荐</p>
                <p className="text-xs mt-1">
                  {recsMeta.hint
                    || (!analysisOn
                      ? '点击开始分析后，系统会自动扫描滚球并生成推荐'
                      : '当前没有通过的推荐')
                    || '请先在赛事页同步滚球'}
                </p>
                {recsMeta.rawCount > 0 ? (
                  <p className="text-xs text-ink-400 mt-2">
                    已分析 {recsMeta.rawCount} 场，只显示可下单推荐
                  </p>
                ) : null}
              </>
            )}
          </div>
        ) : (
          <div className="space-y-3">
            {recommendations.map((rec) => {
              const rec_data = rec.recommendation || {}
              const markets = rec.markets || []
              const bettable = !!rec_data.should_bet
              const readableReason = formatAiRecommendationReason({
                recommendation: rec_data,
                analysis: rec.analysis,
                strategy: rec.strategy,
              })
              const isManual = (engineStatus?.bet_mode || betMode) !== 'active'
              const winRate = rec_data.win_rate ?? ((rec_data.confidence || 0) * 100)
              const visibleMarkets = markets.filter((m) => {
                return m.key === 'ft_ou' || m.bet_type === 'total'
              })
              return (
                <div key={rec.match_id} className="bg-white border border-gray-200 rounded-lg p-4">
                  <div className="flex items-center justify-between mb-3">
                    <div>
                      <div className="font-medium">{rec.home_team} vs {rec.away_team}</div>
                      <div className="text-xs text-ink-500 mt-0.5">
                        {rec.league || `赛事 #${rec.match_id}`}
                        <span className="mx-1.5">·</span>
                        全场小球 · {SITE_NAMES[siteTab] || siteTab}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className={`text-xs font-semibold mb-1 ${bettable ? 'text-brand-700' : 'text-ink-400'}`}>
                        {bettable ? '可下单' : '未放行'}
                      </div>
                      <div className={`text-sm font-bold ${
                        winRate >= 75 ? 'text-brand-700' :
                        winRate >= 60 ? 'text-amber-600' : 'text-red-600'
                      }`}>
                        置信度 {Number(winRate).toFixed(0)}%
                      </div>
                    </div>
                  </div>

                  {/* 全场小球盘口 */}
                  {visibleMarkets.length > 0 ? (
                    <div className="overflow-x-auto mb-3">
                      <div
                        className="grid gap-px bg-ink-200 rounded-lg overflow-hidden min-w-[520px]"
                        style={{ gridTemplateColumns: `repeat(${visibleMarkets.length}, minmax(88px, 1fr))` }}
                      >
                        {visibleMarkets.map((m) => (
                          <div
                            key={m.key}
                            className={`bg-ink-50 px-2 py-1.5 text-center text-xs font-semibold ${
                              m.key.startsWith('ht_') || m.key === 'team_total' ? 'text-sky-700' : 'text-ink-700'
                            }`}
                          >
                            <div>{m.label}</div>
                            {formatOpeningHint(m) ? (
                              <div className="text-[10px] font-normal text-ink-400 mt-0.5 tabular-nums">
                                {formatOpeningHint(m)}
                              </div>
                            ) : null}
                          </div>
                        ))}
                        {[0].flatMap((rowIdx) =>
                          visibleMarkets.map((m) => {
                            const cell = (m.cells || [])[rowIdx]
                            if (!cell) {
                              return (
                                <div key={`${m.key}-r${rowIdx}`} className="bg-white px-2 py-2 text-center text-ink-300 text-sm">
                                  -
                                </div>
                              )
                            }
                            const hi = (m.highlight || []).includes(cell.selection)
                            const isPrimary = m.single?.selection === cell.selection
                            const oddsUp = hi && isPrimary
                            const oddsDown = hi && !isPrimary
                            return (
                              <button
                                key={`${m.key}-${cell.selection}-${rowIdx}`}
                                type="button"
                                disabled={!isManual || !cell.available}
                                onClick={() => handlePlaceCell(rec, m, cell)}
                                className={`px-2 py-2 text-center transition-colors ${
                                  !cell.available ? 'bg-white opacity-40 cursor-default' : 'bg-white hover:bg-ink-50 cursor-pointer'
                                } ${oddsUp ? '!bg-brand-50 ring-1 ring-inset ring-emerald-200' : ''} ${
                                  ''
                                } ${oddsDown ? '!bg-rose-50' : ''}`}
                              >
                                {!cell.available ? (
                                  <span className="text-ink-300 text-sm">-</span>
                                ) : (
                                  <>
                                    <div className="text-xs text-sky-600 font-medium leading-tight">
                                      {formatCellLine(m, cell)}
                                    </div>
                                    <div className={`text-sm font-bold leading-tight ${
                                      oddsUp ? 'text-brand-700' : oddsDown ? 'text-rose-600' : 'text-ink-800'
                                    }`}>
                                      {Number(cell.odds).toFixed(2)}
                                    </div>
                                    {cell.win_rate != null ? (
                                      <div className={`text-[10px] tabular-nums ${oddsUp ? 'text-brand-700' : 'text-ink-500'}`}>
                                        置信度 {Number(cell.win_rate).toFixed(0)}%
                                      </div>
                                    ) : null}
                                    {cell.provider ? (
                                      <div className="text-[10px] text-ink-400 truncate">{cell.provider}</div>
                                    ) : null}
                                  </>
                                )}
                              </button>
                            )
                          })
                        )}
                      </div>
                    </div>
                  ) : null}

                  <div className={`text-xs rounded p-2 mb-3 ${
                    bettable ? 'text-brand-800 bg-brand-50' : 'text-amber-800 bg-amber-50'
                  }`}>
                    <Shield size={12} className="inline mr-1" />
                    {readableReason || '暂无分析说明'}
                  </div>

                  {isManual && (
                    <div className="flex flex-wrap items-center gap-2 pt-2 border-t border-ink-100">
                      <label className="text-xs text-ink-500">金额</label>
                      <input
                        type="number"
                        min={minBetAmount}
                        max={Number(formData.max_bet_amount) || undefined}
                        step="1"
                        className="input w-28 py-1.5 text-sm"
                        value={stakeByMatch[rec.match_id] ?? Number(formData.max_bet_amount || minBetAmount || 1)}
                        onChange={(e) => setStakeByMatch((s) => ({
                          ...s,
                          [rec.match_id]: Number(e.target.value) || 0,
                        }))}
                      />
                      <button
                        type="button"
                        className="btn-primary px-4 py-1.5 text-sm"
                        disabled={!!bettingId || !markets.some((m) => m.single)}
                        onClick={() => handlePlaceFromRec(rec)}
                      >
                        {bettingId && String(bettingId).startsWith(String(rec.match_id)) ? (
                          <Loader2 size={14} className="animate-spin" />
                        ) : (
                          `一键投注`
                        )}
                      </button>
                      <span className="text-xs text-ink-400">
                        {rec_data.selection_label || '-'}
                        {rec_data.line != null && rec_data.line !== '' ? ` ${rec_data.line}` : ''}
                        {' '}@ {Number(rec_data.odds || 0).toFixed(2) || '-'}
                        {winRate ? ` · 置信度 ${Number(winRate).toFixed(0)}%` : ''}
                        {rec_data.provider ? ` · ${rec_data.provider}` : ''}
                      </span>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}
      </div>

      <div className="mt-8 card bg-brand-50/50 border-brand-100/80">
          <h4 className="section-title mb-3 flex items-center gap-2 text-brand-900">
            <Zap size={14} className="text-brand-700" />
            使用说明
          </h4>
        <ol className="panel-note space-y-1.5 list-decimal list-inside text-ink-600">
          <li>上方“启动自动下单”只在自动模式下使用，会直接自动下单</li>
          <li>下方“开始分析”只在人工模式下使用，只生成推荐，不会自动下单</li>
          <li>自动下单和手动分析不会同时运行，切模式时会自动停掉另一条链路</li>
          <li>系统每 {formatIntervalLabel(scanIntervalSec)} 扫描一次滚球，人工模式下可以一键下单</li>
        </ol>
      </div>
    </div>
  )
}
