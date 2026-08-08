import { useState, useEffect, useCallback, useRef } from 'react'
import { aiAPI, betsAPI, adminAPI } from '../lib/api.js'
import { formatAiRecommendationReason } from '../lib/aiReasoning.js'
import { SITE_NAMES, SITE_ORDER } from '../lib/sites.js'
import { useAuth } from '../store/auth.jsx'
import { usePagePoll } from '../hooks/usePagePoll.js'
import { pushLog as addAiLog } from '../store/aiLogs.jsx'
import PageHeader from '../components/PageHeader.jsx'
import BetModeSwitch from '../components/BetModeSwitch.jsx'
import toast from 'react-hot-toast'
import {
  Bot, Play, Square, Settings, Loader2, Shield,
  AlertTriangle, Zap, Brain,
  ChevronDown, ChevronUp, Database, Power,
} from 'lucide-react'

const SEL_LABEL = {
  over: '大',
  under: '小',
  home: '主',
  away: '客',
  draw: '平',
}

const MARKET_LABEL = {
  total: '亚洲大小',
  moneyline: '独赢',
  spread: '亚洲让球',
}

const SPORT_TABS = [
  { key: 'football', label: '足球' },
  { key: 'basketball', label: '篮球' },
]

const SITE_TABS = SITE_ORDER.map((code) => ({ key: code, label: SITE_NAMES[code] }))

const FALLBACK_STRATEGIES = {
  high_win_rate: { description: 'AI 大小球分析' },
}

const DEFAULT_FORM_DATA = {
  is_active: false,
  max_bet_amount: 50,
  max_daily_bets: 3,
  preferred_sports: [],
  excluded_teams: [],
  stop_loss: 500,
  take_profit: 1000,
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
  }
}

function formatCellLine(market, cell) {
  const line = market?.line
  const sel = cell?.selection
  if (sel === 'over' || sel === 'under') {
    const tag = sel === 'over' ? '大' : '小'
    return line != null && line !== '' ? `${tag} ${line}` : `${tag} -`
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

export default function AIPanelPage() {
  const { user, updateUser } = useAuth()
  const [engineStatus, setEngineStatus] = useState(null)
  const [config, setConfig] = useState(null)
  const [strategies, setStrategies] = useState(FALLBACK_STRATEGIES)
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
      console.log('[AI Update]', detail)
      if (detail.type === 'ai_cycle_complete') {
        const n = detail.data?.executed || 0
        const analyzed = detail.data?.analyzed || 0
        toast.success(`AI完成一轮分析: ${analyzed}场分析, ${n}笔下单`)
        loadRecommendations(false)
      } else if (detail.type === 'ai_cycle_done') {
        const analyzed = detail.data?.analyzed || 0
        const msg = detail.data?.message || '本轮无 AI 大小球推荐通过'
        toast(`AI分析完成: ${analyzed}场, 无下单`, { icon: '📊' })
      } else if (detail.type === 'ai_risk_stop') {
        toast.error(`AI引擎暂停: ${detail.data}`)
        setEngineStatus({ running: false })
      } else if (detail.type === 'ai_manual_recommend') {
        toast(`人工模式推荐: ${detail.data.selection} @ ${detail.data.odds}`, { icon: '🤖' })
        loadRecommendations(false)
      } else if (detail.type === 'ai_recs_ready') {
        loadRecommendations(false)
      } else if (detail.type === 'ai_config_updated') {
        toast.success('配置已更新，正在按新参数重新分析…')
        const d = detail.data || {}
        setConfig((prev) => ({ ...(prev || {}), ...d }))
        setFormData((prev) => normalizeFormData({ ...prev, ...d }, prev))
        loadRecommendations(true)
      } else if (detail.type === 'ai_bet_placed') {
        toast.success(`真实下单成功: ${detail.data.selection} @ ${detail.data.odds}`)
      } else if (detail.type === 'ai_bet_failed') {
        toast.error(detail.data?.message || 'AI下单失败')
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
      toast.success('已开始后台分析')
      addAiLog('analysis', `开始后台分析: ${sportTab === 'football' ? '足球' : '篮球'} · ${SITE_NAMES[siteTab] || siteTab}`)
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
      toast.success('已停止分析')
      addAiLog('analysis', '停止后台分析')
      await loadRecommendations(false, sportTab, siteTab, { silent: true })
    } catch (err) {
      toast.error(err?.detail || err?.message || '停止分析失败')
    } finally {
      setAnalysisBusy(false)
    }
  }

  // 后台分析开启时：短轮询拉取进度与 AI 大小球推荐
  usePagePoll(
    () => loadRecommendations(false, sportTab, siteTab, { silent: true }),
    4000,
    { enabled: analysisOn },
  )

  const placeOneLeg = async ({ matchId, betType, selection, odds, stake, provider }) => {
    const bt = String(betType || 'total').toLowerCase()
    const allowed = {
      total: ['over', 'under'],
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

  // 一键投注：大小球下注
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
      toast.error('仅可投注大小球盘口')
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
      const [statusRes, configRes, strategiesRes] = await Promise.all([
        aiAPI.status(),
        aiAPI.config(),
        aiAPI.strategies(),
      ])
      setEngineStatus(statusRes.data || {})
      setBetMode(statusRes.data?.bet_mode || 'manual')
      setConfig(configRes.data)
      const strat = strategiesRes.data && Object.keys(strategiesRes.data).length
        ? strategiesRes.data
        : FALLBACK_STRATEGIES
      setStrategies(strat)

      const c = configRes.data || {}
      // 最低金额跟 AI 配置：默认 1，上限为单笔最大金额
      const minBet = Number(c.one_click_min_stake ?? c.min_bet_amount ?? 1)
      setMinBetAmount(minBet > 0 ? minBet : 1)
      // 清空旧的动态金额（如 2.69），改回按配置单笔上限展示
      setStakeByMatch({})
      setFormData(normalizeFormData(c))
    } catch (err) {
      console.error('Load AI data failed:', err)
      setStrategies(FALLBACK_STRATEGIES)
    } finally {
      setLoading(false)
    }
  }

  const handleStart = async () => {
    setStarting(true)
    try {
      const res = await aiAPI.start()
      toast.success(res.message || '自动下注引擎已启动')
      addAiLog('engine', `自动下注引擎已启动 (${res.data?.effective_label || '运行中'})`)
      setEngineStatus(res.data || { engine_running: true })
      setBetMode(res.data?.bet_mode || betMode)
      updateUser({ ai_enabled: true })
      loadRecommendations(true)
    } catch (err) {
      toast.error(err.detail || '启动失败')
    } finally {
      setStarting(false)
    }
  }

  const handleStop = async () => {
    try {
      const res = await aiAPI.stop()
      toast.success('自动下注引擎已停止')
      addAiLog('engine', '自动下注引擎已停止')
      setEngineStatus(res.data || { engine_running: false, ai_enabled: false })
      updateUser({ ai_enabled: false })
    } catch (err) {
      toast.error('停止失败')
    }
  }

  const handleSaveConfig = async () => {
    try {
      await aiAPI.updateConfig(formData)
      toast.success('AI配置已保存，参数立即生效')
      setShowSettings(false)
      await loadAll()
      // 清旧推荐，按新配置重新分析
      await loadRecommendations(true)
    } catch (err) {
      toast.error('保存失败')
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
  const badgeTone = engineStatus?.badge_tone || 'slate'
  const statusTitle = engineStatus?.effective_label || (engineRunning ? (isAuto ? '自动运行中' : '人工运行中') : 'AI未启动')
  const statusDesc = engineStatus?.effective_description || (
    engineRunning
      ? (isAuto ? '后台持续分析中，命中大小球推荐后会自动真实下单。' : '后台持续分析中，只生成推荐，不会自动下单。')
      : '当前分析/下注引擎未运行。'
  )
  const dsLast = dsMeta?.last_result
  const dsLastText = dsLast?.finished_at
    ? new Date(Number(dsLast.finished_at) * 1000).toLocaleString('zh-CN', { hour12: false })
    : ''

  return (
    <div className="page">
      <PageHeader
        eyebrow="智能"
        title="AI 投注"
        description={`OB / 平博滚球 · 亚洲大小球 · 捷报数据对比 · AI 每 10 分钟扫描一次`}
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
            {!engineRunning ? (
              <button
                onClick={handleStart}
                disabled={starting || !engineStatus}
                className="btn-success flex items-center gap-2 px-6"
              >
                {starting || !engineStatus ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
                {engineStatus ? '启动自动下注引擎' : '加载中…'}
              </button>
            ) : (
              <button
                onClick={handleStop}
                className="btn-danger flex items-center gap-2 px-6"
              >
                <Square size={16} /> 停止自动下注引擎
              </button>
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
              <div className="font-semibold text-ink-900 text-sm">数据源（捷报比分）</div>
              <div className="text-xs text-ink-500">
                {dsEnabled === null ? '加载中…' : dsEnabled
                  ? '已开启 · 每小时自动预取当日全量赛事上下文'
                  : '已关闭 · 仅使用现有缓存，不自动更新捷报数据'}
              </div>
              {dsEnabled && dsLastText ? (
                <div className="text-[11px] text-ink-400 mt-1">
                  {dsLast?.ok === false
                    ? `上次自动预取失败 · ${dsLastText} · ${dsLast.error || '未知错误'}`
                    : `上次自动预取完成 · ${dsLastText} · 足球 ${dsLast?.football_cached || 0} 场 / 篮球 ${dsLast?.basketball_cached || 0} 场`}
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
              {dsEnabled === null ? '加载中…' : dsEnabled ? '已开启' : '已关闭'}
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
            {engineStatus === null ? '⚪ 正在加载…' : statusTitle}
          </span>
          {engineRunning && (
            <span className="text-xs text-ink-500 ml-auto">
              AI 每 10 分钟扫描一次，每轮最多 3 场不同比赛大小球
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
          <h3 className="font-bold mb-4">AI 配置</h3>

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
                开启 AI
              </label>
              <p className="text-xs text-ink-600 mt-1">开启后 AI 引擎按配置自动分析大小球</p>
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
            实时 AI 推荐
          </h3>
          <div className="flex flex-wrap items-center gap-2">
            {!analysisOn ? (
              <button
                type="button"
                onClick={handleStartAnalysis}
                disabled={analysisBusy}
                className="btn-success flex items-center gap-1.5 px-3 py-1.5 text-sm"
              >
                {analysisBusy ? <Loader2 size={14} className="animate-spin" /> : <Play size={14} />}
                开始实时分析
              </button>
            ) : (
              <button
                type="button"
                onClick={handleStopAnalysis}
                disabled={analysisBusy}
                className="btn-danger flex items-center gap-1.5 px-3 py-1.5 text-sm"
              >
                {analysisBusy ? <Loader2 size={14} className="animate-spin" /> : <Square size={14} />}
                停止实时分析
              </button>
            )}
            <button
              onClick={() => loadRecommendations(false, sportTab, siteTab)}
              disabled={recsLoading}
              className="text-sm text-brand-700 hover:text-brand-300 disabled:opacity-50 px-2"
            >
              {recsLoading ? '加载中…' : '刷新列表'}
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
          <span className="text-xs text-ink-400 ml-1">滚球 · 亚洲盘 · 捷报数据对比</span>
        </div>

        {analysisOn && (
          <div className="mb-3 flex items-center gap-2 text-sm text-ink-600 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2">
            {analyzing ? (
              <Loader2 size={16} className="animate-spin text-brand-600 shrink-0" />
            ) : (
              <span className="w-2.5 h-2.5 rounded-full bg-brand-500 animate-pulse shrink-0" />
            )}
            <span>
              {analyzing ? '正在后台轮询滚球，按亚洲盘口和捷报数据对比后刷新推荐' : '后台分析已开启，推荐会自动刷新'}
              {SPORT_TABS.find((t) => t.key === sportTab)?.label || ''}
              {SITE_NAMES[siteTab] ? ` · ${SITE_NAMES[siteTab]}` : ''}
              {recsMeta.total > 0 ? `（${recsMeta.progress}/${recsMeta.total}）` : ''}
            </span>
          </div>
        )}

        {recsLoading && recommendations.length === 0 && !analyzing ? (
          <div className="text-center py-10 text-ink-500">
            <Loader2 size={32} className="mx-auto mb-3 animate-spin text-brand-500" />
            <p>正在加载{SPORT_TABS.find((t) => t.key === sportTab)?.label}实时推荐…</p>
          </div>
        ) : recommendations.length === 0 ? (
          <div className="text-center py-10 text-ink-500">
            {analyzing ? (
              <>
                <Loader2 size={40} className="mx-auto mb-3 animate-spin text-brand-500" />
                <p>正在分析赛事页中的滚球比赛，并比对亚洲盘口与捷报数据后生成推荐</p>
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
                      ? '点击「开始实时分析」后，后台会扫描全部滚球，并按亚洲盘口 + 捷报数据对比生成大小球推荐'
                      : '暂无通过 AI 大小球分析的推荐')
                    || '请先在「赛事」页同步滚球'}
                </p>
                {recsMeta.rawCount > 0 ? (
                  <p className="text-xs text-ink-400 mt-2">
                    已分析 {recsMeta.rawCount} 场，通过 AI 大小球分析的推荐才会显示
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
                        大小球 · {SITE_NAMES[siteTab] || siteTab}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className={`text-xs font-semibold mb-1 ${bettable ? 'text-brand-700' : 'text-ink-400'}`}>
                        {bettable ? '可投注' : '未放行'}
                      </div>
                      <div className={`text-sm font-bold ${
                        winRate >= 75 ? 'text-brand-700' :
                        winRate >= 60 ? 'text-amber-600' : 'text-red-600'
                      }`}>
                        胜率 {Number(winRate).toFixed(0)}%
                      </div>
                    </div>
                  </div>

                  {/* 大小球盘口网格 */}
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
                        {/* 按行渲染：大小球最多 2 行 */}
                        {[0, 1, 2].flatMap((rowIdx) =>
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
                                        胜率 {Number(cell.win_rate).toFixed(0)}%
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
                        {winRate ? ` · 胜率 ${Number(winRate).toFixed(0)}%` : ''}
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
          <li>点「开始实时分析 / 停止实时分析」：只控制后台分析与推荐刷新，不会自动下单</li>
          <li>后台每 10 分钟扫描一次滚球，结合亚洲盘口与捷报数据对比，生成大小球推荐</li>
          <li>点「启动自动下注引擎」后，自动模式才会真实下单；每轮最多放行 3 场不同比赛大小球</li>
          <li>人工模式下可用「一键投注」，AI 大小球分析不通过则拒绝下单</li>
        </ol>
      </div>
    </div>
  )
}
