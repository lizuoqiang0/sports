import { useState, useEffect, useCallback, useRef } from 'react'
import { aiAPI, betsAPI, adminAPI } from '../lib/api.js'
import { SITE_NAMES, SITE_ORDER } from '../lib/sites.js'
import { useAuth } from '../store/auth.jsx'
import { usePagePoll } from '../hooks/usePagePoll.js'
import { pushLog as addAiLog } from '../store/aiLogs.jsx'
import PageHeader from '../components/PageHeader.jsx'
import BetModeSwitch from '../components/BetModeSwitch.jsx'
import toast from 'react-hot-toast'
import {
  Bot, Play, Square, Settings, Loader2, Shield,
  TrendingUp, AlertTriangle, Zap, Brain,
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
  conservative: { description: '保守策略 - 高置信度、小仓位、严风控' },
  balanced: { description: '平衡策略 - 攻守兼备、适中仓位' },
  aggressive: { description: '激进策略 - 高仓位、接受冷门、高回报' },
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
  const [dsLoading, setDsLoading] = useState(false)
  const [prefetchProgress, setPrefetchProgress] = useState(null)
  const [prefetchNullCount, setPrefetchNullCount] = useState(0)
  const prefetchTimerRef = useRef(null)

  const loadDsSwitch = async () => {
    try {
      const res = await adminAPI.getDataSourceSwitch()
      setDsEnabled(res.data?.enabled ?? false)
    } catch {
      setDsEnabled(false)
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
  const [formData, setFormData] = useState({
    strategy: 'balanced',
    max_bet_amount: 100,
    max_daily_bets: 10,
    min_confidence: 0.75,
    preferred_sports: [],
    excluded_teams: [],
    stop_loss: 500,
    take_profit: 1000,
    max_odds: 10,
    min_odds: 1.1,
    use_llm_analysis: true,
    auto_cashout: false,
    cashout_threshold: 0.8,
  })

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
        addAiLog('cycle', `AI 完成一轮分析：${analyzed} 场分析，${n} 笔下单`, detail.data)
        // 输出每场分析结果到日志
        const summary = detail.data?.analysis_summary || []
        for (const m of summary) {
          const sel = SEL_LABEL[m.selection] || m.selection || '-'
          const bt = MARKET_LABEL[m.bet_type] || m.bet_type
          const tag = m.should_bet ? '✓推荐' : `${m.confidence}%`
          addAiLog('analysis', `${m.home_team} vs ${m.away_team}  ${bt} ${sel} @ ${Number(m.odds).toFixed(2)}  ${tag}`)
        }
        loadRecommendations(false)
      } else if (detail.type === 'ai_cycle_done') {
        const analyzed = detail.data?.analyzed || 0
        const msg = detail.data?.message || '本轮无策略通过场次'
        toast(`AI分析完成: ${analyzed}场, 无下单`, { icon: '📊' })
        addAiLog('cycle', `AI 分析完成：${analyzed} 场，无符合策略的下单机会`)
        // 输出每场分析结果到日志
        const summary = detail.data?.analysis_summary || []
        for (const m of summary) {
          const sel = SEL_LABEL[m.selection] || m.selection || '-'
          const bt = MARKET_LABEL[m.bet_type] || m.bet_type
          addAiLog('analysis', `${m.home_team} vs ${m.away_team}  ${bt} ${sel} @ ${Number(m.odds).toFixed(2)}  ${m.confidence}%`)
        }
      } else if (detail.type === 'ai_risk_stop') {
        toast.error(`AI引擎暂停: ${detail.data}`)
        addAiLog('risk', `风控触发: ${detail.data}`)
        setEngineStatus({ running: false })
      } else if (detail.type === 'ai_manual_recommend') {
        toast(`人工模式推荐: ${detail.data.selection} @ ${detail.data.odds}`, { icon: '🤖' })
        addAiLog('recommend', `推荐: ${detail.data.selection} @ ${Number(detail.data.odds).toFixed(2)}`, detail.data)
        loadRecommendations(false)
      } else if (detail.type === 'ai_recs_ready') {
        loadRecommendations(false)
      } else if (detail.type === 'ai_config_updated') {
        toast.success('策略已更新，正在按新参数重新分析…')
        addAiLog('config', 'AI 策略配置已更新')
        const d = detail.data || {}
        setFormData((prev) => ({
          ...prev,
          strategy: d.strategy ?? prev.strategy,
          min_confidence: d.min_confidence ?? prev.min_confidence,
          min_odds: d.min_odds ?? prev.min_odds,
          max_odds: d.max_odds ?? prev.max_odds,
          max_bet_amount: d.max_bet_amount ?? prev.max_bet_amount,
          max_daily_bets: d.max_daily_bets ?? prev.max_daily_bets,
          stop_loss: d.stop_loss ?? prev.stop_loss,
          take_profit: d.take_profit ?? prev.take_profit,
          use_llm_analysis: d.use_llm_analysis ?? prev.use_llm_analysis,
        }))
        loadRecommendations(true)
      } else if (detail.type === 'ai_bet_placed') {
        toast.success(`真实下单成功: ${detail.data.selection} @ ${detail.data.odds}`)
        addAiLog('bet_placed', `下单成功: ${detail.data.selection} @ ${Number(detail.data.odds).toFixed(2)}`, detail.data)
      } else if (detail.type === 'ai_bet_failed') {
        toast.error(detail.data?.message || 'AI下单失败')
        addAiLog('bet_failed', `下单失败: ${detail.data?.message || '未知原因'}`, detail.data)
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
      // 金额默认取 AI 策略「单笔最大金额」（见输入框 value ?? formData.max_bet_amount）
      // 不把凯利 suggested_stake 写入 state，避免出现 2.69 这类偏离配置的金额
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

  // 后台分析开启时：短轮询拉取进度与高胜率结果
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
      throw new Error(`金额需 ≥${minBetAmount}（AI 策略配置）`)
    }
    if (maxStake > 0 && stake > maxStake) {
      throw new Error(`金额需 ≤${maxStake}（策略单笔上限）`)
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
    // 走一键接口：服务端按完整 AI 配置门禁（置信度/赔率/仓位/止损止盈/每日笔数）
    const maxStake = Number(formData.max_bet_amount || 0)
    const stake = Number(stakeByMatch[rec.match_id] || maxStake || minBetAmount || 1)
    if (!stake || stake < minBetAmount) {
      toast.error(`金额需 ≥${minBetAmount}（AI 策略配置）`)
      return
    }
    if (maxStake > 0 && stake > maxStake) {
      toast.error(`金额需 ≤${maxStake}（策略单笔上限）`)
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

  // 一键投注：大小球 + 输赢 + 让球 全部下注
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
      toast.error('仅可投注策略主推盘口')
      return
    }
    if (primarySel && cell.selection !== primarySel) {
      toast.error('仅可投注策略主推方向（已按配置过滤）')
      return
    }
    // 与一键相同：完整策略门禁
    await handlePlaceFromRec(rec)
  }

  const loadAll = async () => {
    setLoading(true)
    try {
      // 策略/配置与推荐解耦，避免推荐超时导致策略下拉空白
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
      // 最低金额跟 AI 策略：默认 1，上限为单笔最大金额（不再用系统 MIN_BET=100）
      const minBet = Number(c.one_click_min_stake ?? c.min_bet_amount ?? 1)
      setMinBetAmount(minBet > 0 ? minBet : 1)
      // 清空旧凯利金额（如 2.69），改回按配置单笔上限展示
      setStakeByMatch({})
      setFormData({
        strategy: c.strategy || 'balanced',
        max_bet_amount: c.max_bet_amount || 100,
        max_daily_bets: c.max_daily_bets || 10,
        min_confidence: c.min_confidence || 0.75,
        preferred_sports: c.preferred_sports || [],
        excluded_teams: c.excluded_teams || [],
        stop_loss: c.stop_loss || 500,
        take_profit: c.take_profit || 1000,
        max_odds: c.max_odds || 10,
        min_odds: c.min_odds || 1.1,
        use_llm_analysis: c.use_llm_analysis !== false,
        auto_cashout: c.auto_cashout || false,
        cashout_threshold: c.cashout_threshold || 0.8,
      })
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
      toast.success(res.message || 'AI引擎已启动')
      addAiLog('engine', `AI 引擎已启动 (${isAuto ? '自动' : '人工'}模式)`)
      setEngineStatus({ running: true, ...(res.data || {}) })
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
      await aiAPI.stop()
      toast.success('AI引擎已停止')
      addAiLog('engine', 'AI 引擎已停止')
      setEngineStatus({ running: false })
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
      // 清旧推荐，按新置信度/赔率/仓位等重新分析
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

  const isAuto = (engineStatus?.bet_mode || betMode) === 'active'

  return (
    <div className="page">
      <PageHeader
        eyebrow="智能"
        title="AI 投注"
        description="OB / 平博滚球 · 亚洲盘（独赢/让球/大小）· 结合初盘与盘口变化 · 胜率门槛 ≥75%"
        actions={(
          <>
            <BetModeSwitch
              onChange={(data) => {
                if (!data) return
                setBetMode(data.bet_mode || 'manual')
                setEngineStatus((s) => ({ ...(s || {}), ...data }))
                // 模式切换后立即按新规则刷列表
                loadRecommendations(false, sportTab, siteTab, { silent: true })
              }}
            />
            {!engineStatus?.running ? (
              <button
                onClick={handleStart}
                disabled={starting || !engineStatus}
                className="btn-success flex items-center gap-2 px-6"
              >
                {starting || !engineStatus ? <Loader2 size={16} className="animate-spin" /> : <Play size={16} />}
                {engineStatus ? '启动 AI' : '加载中…'}
              </button>
            ) : (
              <button
                onClick={handleStop}
                className="btn-danger flex items-center gap-2 px-6"
              >
                <Square size={16} /> 停止
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
                  : '已关闭 · AI 分析时仅读已有缓存'}
              </div>
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
        engineStatus?.running
          ? isAuto
            ? 'border-green-500/30 bg-green-500/5'
            : 'border-yellow-500/30 bg-yellow-500/5'
          : 'border-gray-500/30'
      }`}>
        <div className="flex items-center gap-3">
          <div className={`w-3 h-3 rounded-full ${
            engineStatus?.running
              ? isAuto
                ? 'bg-green-400 animate-pulse'
                : 'bg-yellow-400 animate-pulse'
              : 'bg-gray-500'
          }`} />
          <span className="font-medium">
            {engineStatus?.running
              ? isAuto
                ? '🟢 自动投注 - 每 10 分钟一轮，最多 3 场不同比赛真实下单'
                : '🟡 人工投注 - 轮询分析全部滚球，只展示高胜率供手动确认'
              : engineStatus === null ? '⚪ 正在加载…' : '⚪ AI引擎未运行'}
          </span>
          {engineStatus?.running && (
            <span className="text-xs text-ink-500 ml-auto">
              {isAuto ? '每轮≤2单 · 间隔10分钟' : '轮询扫描滚球大小球'}
            </span>
          )}
        </div>
        {recsMeta.filterHint ? (
          <p className="text-xs text-ink-500 mt-2 pl-6">{recsMeta.filterHint}</p>
        ) : null}
      </div>

      {/* 配置面板 */}
      {showSettings && (
        <div className="card mb-6">
          <h3 className="font-bold mb-4">AI 策略配置</h3>

          <div className="grid md:grid-cols-2 gap-4">
            {/* 策略选择 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">策略预设</label>
              <select
                value={formData.strategy}
                onChange={(e) => setFormData({ ...formData, strategy: e.target.value })}
                className="input"
              >
                {Object.entries(strategies || FALLBACK_STRATEGIES).map(([key, val]) => (
                  <option key={key} value={key}>{val.description || val.name || key}</option>
                ))}
              </select>
            </div>

            {/* 单笔最大金额 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">单笔最大金额</label>
              <input
                type="number"
                value={formData.max_bet_amount}
                onChange={(e) => setFormData({ ...formData, max_bet_amount: Number(e.target.value) })}
                className="input"
                min="10"
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

            {/* 最低置信度 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">
                最低AI置信度: {(formData.min_confidence * 100).toFixed(0)}%
              </label>
              <input
                type="range"
                value={formData.min_confidence}
                onChange={(e) => setFormData({ ...formData, min_confidence: Number(e.target.value) })}
                className="w-full accent-brand-500"
                min="0.5"
                max="0.95"
                step="0.05"
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

            {/* 赔率范围 */}
            <div>
              <label className="block text-sm text-ink-400 mb-1.5">赔率范围</label>
              <div className="flex gap-2">
                <input
                  type="number"
                  value={formData.min_odds}
                  onChange={(e) => setFormData({ ...formData, min_odds: Number(e.target.value) })}
                  className="input"
                  step="0.1"
                  placeholder="最低"
                />
                <span className="self-center text-ink-500">~</span>
                <input
                  type="number"
                  value={formData.max_odds}
                  onChange={(e) => setFormData({ ...formData, max_odds: Number(e.target.value) })}
                  className="input"
                  step="0.5"
                  placeholder="最高"
                />
              </div>
            </div>

            {/* LLM分析 */}
            <div>
              <label className="flex items-center gap-2 text-sm text-ink-400">
                <input
                  type="checkbox"
                  checked={formData.use_llm_analysis}
                  onChange={(e) => setFormData({ ...formData, use_llm_analysis: e.target.checked })}
                  className="accent-brand-500"
                />
                使用LLM深度分析
              </label>
              <p className="text-xs text-ink-600 mt-1">调用大模型分析赛事并生成投注建议</p>
            </div>

            {/* 自动兑现 */}
            <div>
              <label className="flex items-center gap-2 text-sm text-ink-400">
                <input
                  type="checkbox"
                  checked={formData.auto_cashout}
                  onChange={(e) => setFormData({ ...formData, auto_cashout: e.target.checked })}
                  className="accent-brand-500"
                />
                自动提前兑现
              </label>
              <p className="text-xs text-ink-600 mt-1">当赔率达到阈值时自动兑现</p>
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
            今日AI推荐
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
          <span className="text-xs text-ink-400 ml-1">滚球 · 亚洲盘 · 初盘/变盘分析</span>
        </div>

        {analysisOn && (
          <div className="mb-3 flex items-center gap-2 text-sm text-ink-600 bg-amber-50 border border-amber-100 rounded-lg px-3 py-2">
            {analyzing ? (
              <Loader2 size={16} className="animate-spin text-brand-600 shrink-0" />
            ) : (
              <span className="w-2.5 h-2.5 rounded-full bg-brand-500 animate-pulse shrink-0" />
            )}
            <span>
              {analyzing ? '正在后台分析滚球' : '后台分析已开启，轮询更新中'}
              {SPORT_TABS.find((t) => t.key === sportTab)?.label || ''}
              {SITE_NAMES[siteTab] ? ` · ${SITE_NAMES[siteTab]}` : ''}
              {recsMeta.total > 0 ? `（${recsMeta.progress}/${recsMeta.total}）` : ''}
              {recsMeta.minWinRate != null ? ` · 展示胜率≥${Number(recsMeta.minWinRate).toFixed(0)}%` : ''}
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
                <p>正在分析赛事页中的滚球比赛</p>
                <p className="text-xs mt-1">
                  {recsMeta.total > 0
                    ? `进度 ${recsMeta.progress}/${recsMeta.total}，完成后自动显示`
                    : '结果就绪后将自动显示，无需长时间等待本页'}
                </p>
              </>
            ) : (
              <>
                <Bot size={48} className="mx-auto mb-3 opacity-30" />
                <p>暂无{SPORT_TABS.find((t) => t.key === sportTab)?.label}推荐</p>
                <p className="text-xs mt-1">
                  {recsMeta.hint
                    || (!analysisOn
                      ? '点击「开始分析」后后台轮询全部滚球'
                      : `暂无胜率≥${recsMeta.minWinRate != null ? Number(recsMeta.minWinRate).toFixed(0) : '策略'}% 的比赛`)
                    || '请先在「赛事」页同步滚球'}
                </p>
                {recsMeta.rawCount > 0 ? (
                  <p className="text-xs text-ink-400 mt-2">
                    已分析 {recsMeta.rawCount} 场，高于策略胜率阈值的场次才会显示
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
              const isManual = (engineStatus?.bet_mode || betMode) !== 'active'
              const winRate = rec_data.win_rate ?? ((rec_data.confidence || 0) * 100)
              const sportKey = String(rec.sport || sportTab || 'football').toLowerCase()
              const visibleMarkets = markets.filter((m) => {
                if (sportKey === 'basketball') {
                  return m.key === 'ft_ou' || m.bet_type === 'total'
                }
                return ['ft_1x2', 'ft_ah', 'ft_ou'].includes(m.key)
                  || ['moneyline', 'spread', 'total'].includes(m.bet_type)
              })
              const primaryLabel = MARKET_LABEL[rec_data.bet_type] || '大小'
              return (
                <div key={rec.match_id} className="bg-white border border-gray-200 rounded-lg p-4">
                  <div className="flex items-center justify-between mb-3">
                    <div>
                      <div className="font-medium">{rec.home_team} vs {rec.away_team}</div>
                      <div className="text-xs text-ink-500 mt-0.5">
                        {rec.league || `赛事 #${rec.match_id}`}
                        <span className="mx-1.5">·</span>
                        单边 · {sportKey === 'football' ? '独赢/亚洲让球/亚洲大小' : '亚洲大小'} · {SITE_NAMES[siteTab] || siteTab}
                        {rec_data.bet_type ? ` · 主推${primaryLabel}` : ''}
                      </div>
                    </div>
                    <div className="text-right">
                      <div className={`text-xs font-semibold mb-1 ${bettable ? 'text-brand-700' : 'text-ink-400'}`}>
                        {bettable ? '可投注' : '观望'}
                      </div>
                      <div className={`text-sm font-bold ${
                        winRate >= 75 ? 'text-brand-700' :
                        winRate >= 60 ? 'text-amber-600' : 'text-red-600'
                      }`}>
                        胜率 {Number(winRate).toFixed(0)}%
                      </div>
                    </div>
                  </div>

                  {/* 盘口网格（对齐截图：独赢/让球/大小…） */}
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
                        {/* 按行渲染：独赢最多 3 行，其余 2 行 */}
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

                  <div className="text-xs text-ink-400 bg-ink-50 rounded p-2 mb-3">
                    <Shield size={12} className="inline mr-1" />
                    {rec_data.reasoning || rec.analysis?.reasoning || '暂无分析说明'}
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
                        主推{primaryLabel} · {rec_data.selection_label || '-'}
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
          <li>在「今日AI推荐」点「开始分析 / 停止分析」控制后台轮询</li>
          <li>人工与自动：全部按「配置」参数运行（置信度、赔率区间、单笔上限、每日笔数、止损/止盈、偏好球类、排除球队、是否 LLM）</li>
          <li>自动投注还需点「启动 AI」：每 10 分钟一轮，最多下 3 单且为不同比赛</li>
          <li>人工一键投注：同样校验策略，未通过配置则拒绝下单</li>
        </ol>
      </div>
    </div>
  )
}
