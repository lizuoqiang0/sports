import { useState, useEffect, useCallback } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { matchesAPI, oddsAPI, aiAPI, bookmakersAPI } from '../lib/api.js'
import { formatAiRecommendationReason } from '../lib/aiReasoning.js'
import { extractErrorMessage } from '../lib/httpError.js'
import { usePagePoll } from '../hooks/usePagePoll.js'
import { useWebSocket } from '../store/websocket.jsx'
import toast from 'react-hot-toast'
import { formatMoney, sportLabel } from '../lib/format.js'
import {
  ArrowLeft, Bot, Loader2,
  Shield, AlertTriangle, Target
} from 'lucide-react'

export default function MatchDetailPage() {
  const { id } = useParams()
  const navigate = useNavigate()
  const { subscribe, oddsUpdates, matchUpdates } = useWebSocket()
  const [match, setMatch] = useState(null)
  const [odds, setOdds] = useState([])
  const [loading, setLoading] = useState(true)
  const [aiAnalysis, setAiAnalysis] = useState(null)
  const [loadingAI, setLoadingAI] = useState(false)
  const [crossOdds, setCrossOdds] = useState(null)

  const normalizeOdds = (data, fallback = []) => {
    if (Array.isArray(data)) return data
    if (data && Array.isArray(data.items)) return data.items
    if (Array.isArray(fallback)) return fallback
    return []
  }

  const loadData = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true)
    try {
      const [matchRes, oddsRes, compareRes] = await Promise.allSettled([
        matchesAPI.detail(id),
        oddsAPI.getMatch(id),
        bookmakersAPI.oddsCompare(id),
      ])

      if (matchRes.status === 'fulfilled') {
        setMatch(matchRes.value.data)
      }

      if (oddsRes.status === 'fulfilled') {
        const fallbackOdds = matchRes.status === 'fulfilled' ? (matchRes.value.data?.odds || []) : []
        setOdds(normalizeOdds(oddsRes.value.data, fallbackOdds))
      } else if (matchRes.status === 'fulfilled') {
        setOdds(normalizeOdds(matchRes.value.data?.odds || [], []))
      } else {
        setOdds([])
      }

      if (compareRes.status === 'fulfilled') {
        setCrossOdds(compareRes.value?.data || null)
      } else {
        setCrossOdds(null)
      }

      if (matchRes.status !== 'fulfilled') {
        throw (matchRes.reason || new Error('赛事信息加载失败'))
      }
      if (!silent && (oddsRes.status !== 'fulfilled' || compareRes.status !== 'fulfilled')) {
        toast.error('部分数据加载失败，已先显示赛事基础信息')
      }
    } catch (err) {
      if (!silent) toast.error(extractErrorMessage(err, '赛事详情加载失败，请稍后重试'))
    } finally {
      if (!silent) setLoading(false)
    }
  }, [id])

  useEffect(() => {
    subscribe(Number(id))
    loadData()
    // eslint-disable-next-line react-hooks/exhaustive-deps -- remount on match id only
  }, [id])

  usePagePoll(() => loadData({ silent: true }), 15000)

  useEffect(() => {
    const u = matchUpdates?.[Number(id)]
    if (!u || !match) return
    setMatch((prev) => (prev ? {
      ...prev,
      home_score: u.home_score ?? prev.home_score,
      away_score: u.away_score ?? prev.away_score,
      clock: u.clock ?? prev.clock,
      period: u.period ?? prev.period,
      status: u.status || prev.status,
    } : prev))
    if (Array.isArray(u.odds) && u.odds.length) {
      setOdds((prev) => {
        const byType = Object.fromEntries((prev || []).map((o) => [o.bet_type, o]))
        return u.odds.map((o) => ({
          ...(byType[o.bet_type] || {}),
          ...o,
          odds_data: { ...(byType[o.bet_type]?.odds_data || {}), ...(o.odds_data || {}) },
        }))
      })
    }
  }, [matchUpdates, id])

  useEffect(() => {
    const update = oddsUpdates[Number(id)]
    if (update && Array.isArray(odds) && odds.length) {
      setOdds((prev) => {
        if (!Array.isArray(prev)) return prev
        return prev.map((o) => {
          if ((o.bet_type === 'moneyline' || o.bet_type === 'Moneyline') && update.home !== undefined) {
            const { _updatedAt, ...rest } = update
            return { ...o, odds_data: { ...o.odds_data, ...rest } }
          }
          return o
        })
      })
    }
  }, [oddsUpdates])

  const loadAIAnalysis = async () => {
    setLoadingAI(true)
    try {
      const res = await aiAPI.recommend(id)
      setAiAnalysis(res.data)
    } catch (err) {
      toast.error(extractErrorMessage(err, 'AI 分析加载失败，请稍后重试'))
    } finally {
      setLoadingAI(false)
    }
  }

  if (loading && !match) {
    return (
      <div className="flex items-center justify-center h-full">
        <Loader2 size={32} className="animate-spin text-brand-500" />
      </div>
    )
  }

  if (!match) return <div className="page text-ink-400">赛事不存在</div>

  const oddsList = Array.isArray(odds) ? odds : []
  const moneylineRows = oddsList.filter((o) => o.bet_type === 'moneyline' || o.bet_type === 'Moneyline')
  const spreadRows = oddsList.filter((o) => o.bet_type === 'spread' || o.bet_type === 'Spread')
  const totalRows = oddsList.filter((o) => o.bet_type === 'total' || o.bet_type === 'Total')
  const clockLabel = [match.period, match.clock].filter(Boolean).join(' · ')
  const readableAiReason = aiAnalysis
    ? formatAiRecommendationReason({
        recommendation: aiAnalysis.recommendation,
        analysis: aiAnalysis.analysis,
        strategy: aiAnalysis.strategy,
      })
    : ''

  return (
    <div className="page max-w-4xl">
      <button
        type="button"
        onClick={() => navigate(-1)}
        className="btn-ghost px-2 py-1.5 mb-5 -ml-2 text-ink-500"
      >
        <ArrowLeft size={16} /> 返回赛事
      </button>

      <div className="card mb-6">
        <div className="flex items-center gap-2 mb-5">
          <span className={`badge ${
            match.status === 'live' ? 'badge-live' :
            match.status === 'upcoming' ? 'badge-upcoming' : 'badge-finished'
          }`}>
            {match.status === 'live' ? 'LIVE' : match.status === 'upcoming' ? '未开始' : '已结束'}
          </span>
          {clockLabel ? (
            <span className="text-sm font-semibold text-rose-600 tabular-nums">{clockLabel}</span>
          ) : null}
          <span className="text-sm text-ink-400">{match.league}</span>
          <span className="text-sm text-ink-400">· {sportLabel(match.sport)}</span>
        </div>

        <div className="flex items-center justify-between">
          <div className="text-center flex-1">
            <div className="text-2xl font-semibold tracking-tight text-ink-900">{match.home_team}</div>
            <div className="text-3xl font-semibold mt-2 text-brand-700 tabular-nums">{match.home_score ?? 0}</div>
          </div>
          <div className="text-ink-400 px-6 text-center">
            <div className="text-sm font-semibold tracking-wide">VS</div>
          </div>
          <div className="text-center flex-1">
            <div className="text-2xl font-semibold tracking-tight text-ink-900">{match.away_team}</div>
            <div className="text-3xl font-semibold mt-2 text-brand-700 tabular-nums">{match.away_score ?? 0}</div>
          </div>
        </div>

        <div className="mt-4 flex items-center justify-between border-t border-gray-200 pt-4">
          <button
            onClick={loadAIAnalysis}
            disabled={loadingAI}
            className="btn-outline flex items-center gap-2"
          >
            {loadingAI ? <Loader2 size={16} className="animate-spin" /> : <Bot size={16} />}
            获取AI分析
          </button>
          {aiAnalysis && (
            <div className="flex items-center gap-3">
              <span className="text-sm text-gray-400">AI推荐:</span>
              <span className={`font-bold ${
                aiAnalysis.recommendation?.selection === 'home' ? 'text-brand-700' :
                aiAnalysis.recommendation?.selection === 'away' ? 'text-sky-700' : 'text-amber-600'
              }`}>
                {aiAnalysis.recommendation?.selection === 'home' ? match.home_team :
                 aiAnalysis.recommendation?.selection === 'away' ? match.away_team :
                 aiAnalysis.recommendation?.selection === 'over' ? '大球' :
                 aiAnalysis.recommendation?.selection === 'under' ? '小球' : '平局'}
              </span>
              <span className="text-sm text-gray-500">
                置信度: {((aiAnalysis.recommendation?.confidence || 0) * 100).toFixed(0)}%
              </span>
            </div>
          )}
        </div>
      </div>

      {crossOdds?.best && Object.keys(crossOdds.best).length > 0 && (
        <div className="card mb-6">
          <h3 className="font-bold text-gray-900 mb-3">跨站最优赔率</h3>
          <div className="grid grid-cols-3 gap-3">
            {Object.entries(crossOdds.best).map(([sel, info]) => (
              <div key={sel} className="rounded-lg border border-gray-200 bg-gray-50 p-3 text-center">
                <div className="text-xs text-gray-500 mb-1">{sel}</div>
                <div className="text-xl font-bold text-gray-900">{Number(info.odds).toFixed(2)}</div>
                <div className="text-xs text-brand-700 mt-1">{info.provider}</div>
              </div>
            ))}
          </div>
        </div>
      )}

      {aiAnalysis && (
        <div className="card mb-6 border-brand-500/30">
          <div className="flex items-center gap-2 mb-3">
            <Bot size={18} className="text-brand-700" />
            <h3 className="font-bold">AI 深度分析</h3>
            <span className={`badge ml-auto ${
              aiAnalysis.analysis?.risk_level === 'low' ? 'bg-brand-50 text-brand-700' :
              aiAnalysis.analysis?.risk_level === 'high' ? 'bg-red-50 text-red-600' :
              'bg-amber-50 text-amber-600'
            }`}>
              {aiAnalysis.analysis?.risk_level === 'low' ? '低风险' :
               aiAnalysis.analysis?.risk_level === 'high' ? '高风险' : '中风险'}
            </span>
          </div>

          <div className="space-y-3">
            <div className="bg-white rounded-lg p-3">
              <div className="text-sm text-gray-400 mb-1">分析结论</div>
              <p className="text-sm">{readableAiReason || aiAnalysis.recommendation?.reasoning}</p>
            </div>

            <div className="grid grid-cols-4 gap-3">
              <MetricCard
                label="当前赔率"
                value={`${Number(aiAnalysis.recommendation?.odds || 0).toFixed(2)}`}
                icon={Target}
              />
              <MetricCard
                label="建议金额"
                value={`${(aiAnalysis.recommendation?.suggested_stake)?.toFixed(0) || 0}`}
                icon={ArrowLeft}
              />
              <MetricCard
                label="置信度"
                value={`${((aiAnalysis.recommendation?.confidence || 0) * 100).toFixed(0)}%`}
                icon={Shield}
              />
              <MetricCard
                label="风险分"
                value={`${((aiAnalysis.recommendation?.risk_score || 0) * 100).toFixed(0)}`}
                positive={false}
                icon={AlertTriangle}
              />
            </div>

            {aiAnalysis.analysis?.key_factors?.length > 0 && (
              <div>
                <div className="text-sm text-gray-400 mb-2">关键因素</div>
                <div className="flex flex-wrap gap-2">
                  {aiAnalysis.analysis.key_factors.map((f, i) => (
                    <span key={i} className="px-2 py-1 bg-brand-50 border border-brand-200 rounded text-xs text-brand-700">
                      {f}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* 仅展示盘口，无下单 */}
      <div className="card">
        <h3 className="font-bold mb-4">盘口 · 亚洲盘（仅供查看）</h3>

        {moneylineRows.length > 0 && (
          <div className="mb-6">
            <div className="text-sm text-gray-400 mb-2">独赢</div>
            <div className="space-y-3">
              {moneylineRows.map((row) => (
                <div key={`ml-${row.provider || row.id || 'x'}`}>
                  <div className="text-xs text-brand-700 mb-1.5">{row.provider || '未知站点'}</div>
                  <div className="grid grid-cols-3 gap-3">
                    <OddsCell label={match.home_team} value={row.odds_data?.home} />
                    <OddsCell label="平局" value={row.odds_data?.draw} />
                    <OddsCell label={match.away_team} value={row.odds_data?.away} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {spreadRows.length > 0 && (
          <div className="mb-6">
            <div className="text-sm text-gray-400 mb-2">亚洲让球</div>
            <div className="space-y-3">
              {spreadRows.map((row) => (
                <div key={`sp-${row.provider || row.id || 'x'}`}>
                  <div className="text-xs text-brand-700 mb-1.5">
                    {row.provider || '未知站点'}
                    {row.spread != null ? ` · ${row.spread > 0 ? '+' : ''}${row.spread}` : ''}
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <OddsCell label={match.home_team} value={row.odds_data?.home} />
                    <OddsCell label={match.away_team} value={row.odds_data?.away} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {totalRows.length > 0 && (
          <div>
            <div className="text-sm text-gray-400 mb-2">亚洲大小</div>
            <div className="space-y-3">
              {totalRows.map((row) => (
                <div key={`tot-${row.provider || row.id || 'x'}`}>
                  <div className="text-xs text-brand-700 mb-1.5">
                    {row.provider || '未知站点'}
                    {row.total != null ? ` · ${row.total}` : ''}
                  </div>
                  <div className="grid grid-cols-2 gap-3">
                    <OddsCell label={`大 ${row.total ?? ''}`} value={row.odds_data?.over} />
                    <OddsCell label={`小 ${row.total ?? ''}`} value={row.odds_data?.under} />
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {!moneylineRows.length && !spreadRows.length && !totalRows.length && (
          <p className="text-sm text-ink-400 text-center py-8">暂无盘口数据</p>
        )}
      </div>
    </div>
  )
}

function OddsCell({ label, value }) {
  if (value == null || value === '') return null
  return (
    <div className="p-3 rounded-lg border border-gray-200 bg-white text-left">
      <div className="text-xs truncate text-gray-500">{label}</div>
      <div className="font-bold text-lg text-gray-900">{Number(value).toFixed(2)}</div>
    </div>
  )
}

function MetricCard({ label, value, positive, icon: Icon }) {
  return (
    <div className="bg-white rounded-lg p-3">
      <div className="flex items-center gap-1.5 mb-1">
        <Icon size={12} className={positive ? 'text-brand-700' : 'text-gray-500'} />
        <span className="text-[10px] text-gray-500">{label}</span>
      </div>
      <div className={`font-bold text-sm ${positive === false ? 'text-red-600' : positive === true ? 'text-brand-700' : ''}`}>
        {value}
      </div>
    </div>
  )
}
