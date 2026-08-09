import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { betsAPI, matchesAPI, aiAPI, bookmakersAPI } from '../lib/api.js'
import { SITE_NAMES } from '../lib/sites.js'
import { usePagePoll } from '../hooks/usePagePoll.js'
import { useAuth } from '../store/auth.jsx'
import { useWebSocket } from '../store/websocket.jsx'
import PageHeader from '../components/PageHeader.jsx'
import {
  TrendingUp, TrendingDown, Wallet,
  Bot, Radio, Loader2
} from 'lucide-react'
import { formatMoney, toNumber } from '../lib/format.js'

export default function DashboardPage() {
  const { user } = useAuth()
  const { connected } = useWebSocket()
  const navigate = useNavigate()

  const [stats, setStats] = useState(null)
  const [liveMatches, setLiveMatches] = useState([])
  const [recentMatches, setRecentMatches] = useState([])
  const [aiStatus, setAiStatus] = useState({ running: false })
  const [siteBalances, setSiteBalances] = useState({ sites: [], total_balance: 0 })
  const [loading, setLoading] = useState(true)

  const loadDashboard = useCallback(async ({ silent = false } = {}) => {
    if (!silent) setLoading(true)
    try {
      const [portRes, liveRes, upRes, aiRes, balRes] = await Promise.all([
        betsAPI.portfolio(),
        matchesAPI.live(),
        matchesAPI.list({ status: 'finished', page: 1, page_size: 5 }),
        aiAPI.status(),
        bookmakersAPI.balances().catch(() => ({ data: { sites: [], total_balance: 0 } })),
      ])
      setStats(portRes.data)
      setLiveMatches(liveRes.data || [])
      setRecentMatches(upRes.data?.items || [])
      setAiStatus(aiRes.data)
      setSiteBalances(balRes.data || { sites: [], total_balance: 0 })
    } catch (err) {
      console.error('Dashboard load error:', err)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadDashboard()
  }, [loadDashboard])

  usePagePoll(
    () => loadDashboard({ silent: true }),
    connected ? 20000 : 10000,
  )

  if (loading && !stats) {
    return (
      <div className="flex items-center justify-center h-full min-h-[50vh]">
        <Loader2 size={28} className="animate-spin text-ink-400" />
      </div>
    )
  }

  const totalAssets = toNumber(stats?.total_assets ?? siteBalances.total_balance)
  const balanceDelta = toNumber(stats?.balance_delta ?? stats?.daily_pnl)
  const referenceBalance = toNumber(stats?.reference_balance ?? stats?.baseline)
  const snapshotUpdatedAt = stats?.snapshot_updated_at
  const aiTone = aiStatus?.badge_tone || 'slate'
  const aiLabel = aiStatus?.effective_label || (aiStatus?.engine_running ? 'AI 运行中' : 'AI 未启动')
  const showAiBadge = Boolean(aiStatus && aiStatus?.effective_state && aiStatus.effective_state !== 'disabled')
  const formatFinishedTime = (match) => {
    const value = match?.finished_at || match?.end_time || match?.updated_at || match?.start_time
    if (!value) return '-'
    try {
      return new Date(value).toLocaleString('zh-CN', {
        month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
      })
    } catch {
      return String(value)
    }
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="概览"
        title="工作台"
        description={`${user?.username || '你好'}，今天是 ${new Date().toLocaleDateString('zh-CN', {
          weekday: 'long', year: 'numeric', month: 'long', day: 'numeric',
        })}`}
        actions={(
          <>
            {showAiBadge && (
              <div className={`flex items-center gap-2 px-3 py-1.5 rounded-lg border ${
                aiTone === 'green'
                  ? 'bg-brand-50 border-brand-100'
                  : aiTone === 'amber'
                    ? 'bg-yellow-50 border-yellow-100'
                    : aiTone === 'orange'
                      ? 'bg-orange-50 border-orange-100'
                      : 'bg-ink-50 border-ink-100'
              }`}>
                <Bot size={15} className={
                  aiTone === 'green'
                    ? 'text-brand-700'
                    : aiTone === 'amber'
                      ? 'text-yellow-700'
                      : aiTone === 'orange'
                        ? 'text-orange-700'
                        : 'text-ink-600'
                } />
                <span className={`text-xs font-semibold ${
                  aiTone === 'green'
                    ? 'text-brand-700'
                    : aiTone === 'amber'
                      ? 'text-yellow-700'
                      : aiTone === 'orange'
                        ? 'text-orange-700'
                        : 'text-ink-600'
                }`}>{aiLabel}</span>
              </div>
            )}
            <div className={`flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg font-semibold border ${
              connected
                ? 'bg-white text-brand-700 border-brand-100'
                : 'bg-white text-rose-600 border-rose-100'
            }`}>
              <div className={`w-1.5 h-1.5 rounded-sm ${connected ? 'bg-brand-500' : 'bg-rose-500'}`} />
              {connected ? '实时行情' : '离线'}
            </div>
          </>
        )}
      />

      {/* 站点余额卡片 */}
      <div className="card mb-7">
        <div className="flex items-center justify-between mb-4">
          <div className="flex items-center gap-2">
            <Wallet size={18} className="text-brand-700" />
            <h3 className="section-title">网站余额</h3>
          </div>
          <div className="text-right">
            <div className="text-xs text-ink-400">合计</div>
            <div className="metric-value text-[1.35rem] text-brand-700">¥{formatMoney(totalAssets)}</div>
          </div>
        </div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          {(siteBalances.sites || []).map((site) => (
            <SiteBalanceCard key={site.code} site={site} />
          ))}
          {(!siteBalances.sites || siteBalances.sites.length === 0) && (
            <div className="col-span-4 text-center py-6 text-ink-400 text-sm">暂无站点数据</div>
          )}
        </div>
      </div>

      {/* 总资产 + 盈亏 */}
      <div className="grid grid-cols-2 gap-4 mb-7">
        <div className="card">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-semibold text-ink-500 tracking-wide">总资产</span>
            <div className="w-8 h-8 rounded-lg bg-ink-50 text-ink-500 flex items-center justify-center">
              <Wallet size={15} strokeWidth={1.8} />
            </div>
          </div>
          <div className="metric-value text-[1.5rem] text-ink-900">¥{formatMoney(totalAssets)}</div>
          <div className="text-xs text-ink-400 mt-1.5">OB + 平博 余额合计</div>
        </div>
        <div className="card">
          <div className="flex items-center justify-between mb-3">
            <span className="text-xs font-semibold text-ink-500 tracking-wide">网站盈亏</span>
            <div className={`w-8 h-8 rounded-lg flex items-center justify-center ${
              balanceDelta >= 0 ? 'bg-brand-50 text-brand-700' : 'bg-rose-50 text-rose-700'
            }`}>
              {balanceDelta >= 0 ? <TrendingUp size={15} strokeWidth={1.8} /> : <TrendingDown size={15} strokeWidth={1.8} />}
            </div>
          </div>
          <div className={`metric-value text-[1.5rem] ${balanceDelta >= 0 ? 'text-brand-700' : 'text-rose-600'}`}>
            {balanceDelta >= 0 ? '+' : ''}¥{formatMoney(balanceDelta)}
          </div>
          <div className="text-xs text-ink-400 mt-1.5">
            {referenceBalance > 0
              ? `按网站余额相对基准 ¥${formatMoney(referenceBalance)} 的增减计算`
              : '按网站余额增减计算'}
          </div>
          {snapshotUpdatedAt && (
            <div className="text-[11px] text-ink-300 mt-1">
              最近快照 {new Date(snapshotUpdatedAt).toLocaleString('zh-CN')}
            </div>
          )}
        </div>
      </div>

      {/* 赛事列表 */}
      <div className="grid md:grid-cols-2 gap-5">
        <div className="card">
          <div className="flex items-center gap-2 mb-4">
            <Radio size={15} className="text-red-500" />
            <h3 className="section-title">进行中</h3>
            <span className="badge-live ml-auto">{liveMatches.length} 场</span>
          </div>
          {liveMatches.length === 0 ? (
            <EmptyRow text="暂无进行中的赛事" />
          ) : (
            <div className="divide-y divide-ink-100">
              {liveMatches.slice(0, 8).map((m) => (
                <button
                  key={m.id}
                  onClick={() => navigate(`/matches/${m.id}`)}
                  className="w-full flex items-center justify-between py-3 text-left hover:bg-ink-50/80 px-1 rounded-lg transition-colors"
                >
                  <div className="text-sm text-ink-800">
                    <span className="font-semibold">{m.home_team}</span>
                    <span className="text-ink-400 mx-2 tabular-nums">{m.home_score} - {m.away_score}</span>
                    <span className="font-semibold">{m.away_team}</span>
                  </div>
                  <span className="text-xs text-ink-400">{m.league}</span>
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="card">
          <div className="flex items-center justify-between mb-4">
            <h3 className="section-title">最近完场</h3>
            <button
              onClick={() => navigate('/matches')}
              className="text-xs font-semibold text-brand-700 hover:text-brand-800"
            >
              查看全部
            </button>
          </div>
          {recentMatches.length === 0 ? (
            <EmptyRow text="暂无完场赛事" />
          ) : (
            <div className="divide-y divide-ink-100">
              {recentMatches.map((m) => (
                <button
                  key={m.id}
                  onClick={() => navigate(`/matches/${m.id}`)}
                  className="w-full flex items-center justify-between py-3 text-left hover:bg-ink-50/80 px-1 rounded-lg transition-colors"
                >
                  <div className="text-sm text-ink-800 min-w-0">
                    <div className="truncate">
                      <span className="font-semibold">{m.home_team}</span>
                      <span className="text-ink-400 mx-2 tabular-nums">{m.home_score} - {m.away_score}</span>
                      <span className="font-semibold">{m.away_team}</span>
                    </div>
                    <div className="text-[11px] text-ink-400 mt-1 truncate">{m.league}</div>
                  </div>
                  <div className="text-xs text-ink-400 tabular-nums">
                    {formatFinishedTime(m)}
                  </div>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function SiteBalanceCard({ site }) {
  const isOnline = ['connected', 'verified'].includes(String(site.status || '').toLowerCase())
  const live = site.live === true
  const name = SITE_NAMES[site.code] || site.name || site.code
  return (
    <div className={`rounded-lg border p-3.5 transition-colors ${
      isOnline
        ? 'bg-brand-50/60 border-brand-100'
        : 'bg-ink-50 border-ink-100'
    }`}>
      <div className="flex items-center justify-between mb-1.5">
        <span className="text-xs font-semibold text-ink-700">{name}</span>
        <span className={`w-1.5 h-1.5 rounded-sm ${isOnline || live ? 'bg-brand-500' : 'bg-ink-300'}`} />
      </div>
      <div className="metric-value text-sm tabular-nums">¥{formatMoney(site.balance || 0)}</div>
      <div className="text-[10px] text-ink-400 mt-0.5">
        {isOnline ? (live ? '在线·实时' : '在线·待刷新') : (live ? '浏览器会话' : '未连接')}
      </div>
    </div>
  )
}

function EmptyRow({ text }) {
  return <div className="text-center py-8 text-ink-400 text-sm">{text}</div>
}
