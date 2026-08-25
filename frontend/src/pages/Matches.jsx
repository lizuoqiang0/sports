import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { matchesAPI, bookmakersAPI, aiAPI } from '../lib/api.js'
import { sortMatchesByDuration } from '../lib/matches.js'
import { SITE_ORDER, SITE_NAMES } from '../lib/sites.js'
import { usePagePoll } from '../hooks/usePagePoll.js'
import { useWebSocket } from '../store/websocket.jsx'
import PageHeader from '../components/PageHeader.jsx'
import { Search, ChevronRight } from 'lucide-react'
import toast from 'react-hot-toast'

const SPORTS = [
  { value: '', label: '全部球类' },
  { value: 'football', label: '足球' },
  { value: 'basketball', label: '篮球' },
]

const SITE_TABS = [
  { value: 'ob', label: SITE_NAMES.ob },
  { value: 'pinnacle', label: SITE_NAMES.pinnacle },
]

export default function MatchesPage() {
  const [matches, setMatches] = useState([])
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [lastUpdated, setLastUpdated] = useState(null)
  const [sport, setSport] = useState('')
  // 默认平博：OB 常未连接时「进行中」会被空列表误导
  const [site, setSite] = useState('pinnacle')
  const [search, setSearch] = useState('')
  const [searchInput, setSearchInput] = useState('')
  const navigate = useNavigate()
  const { subscribe, matchUpdates, oddsUpdates, connected } = useWebSocket()

  const loadMatches = useCallback(async ({ silent = false } = {}) => {
    if (silent) setRefreshing(true)
    else setLoading(true)
    try {
      const res = search
        ? await matchesAPI.search(search)
        : await matchesAPI.list({
            sport: sport || undefined,
            status: 'live',
            provider: site || undefined,
            page: 1,
            page_size: 200,
          })
      let items = res.data?.items || res.data || []
      // 搜索结果再按站点分类过滤
      if (search && site) {
        items = items.filter((m) => (m.site_code || '').toLowerCase() === site)
      }
      setMatches(sortMatchesByDuration(items))
      setLastUpdated(new Date())
      // 预分析赛事页滚球，供 AI 投注页直接展示（后台任务，不阻塞本页）
      if (!search && items.length > 0) {
        const sports = sport
          ? [sport]
          : [...new Set(items.map((m) => m.sport).filter((s) => s === 'football' || s === 'basketball'))]
        for (const sp of sports.slice(0, 2)) {
          aiAPI.prepareRecommendations(sp, 16, site || '').catch(() => {})
        }
      }
    } catch (err) {
      if (!silent) toast.error('赛事加载失败')
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [search, sport, site])

  useEffect(() => {
    loadMatches()
  }, [sport, site])

  // 搜索防抖
  useEffect(() => {
    const timer = setTimeout(() => {
      if (searchInput) {
        handleSearch()
      } else {
        loadMatches()
      }
    }, 500)
    return () => clearTimeout(timer)
  }, [searchInput])

  // HTTP 兜底：WS 已连接时放慢；标签页隐藏时暂停
  usePagePoll(
    () => loadMatches({ silent: true }),
    connected ? 12000 : 5000,
    { enabled: !searchInput },
  )

  // WebSocket 推送：就地合并比分/时钟/赔率，并按时长重排
  useEffect(() => {
    if (!matchUpdates || !Object.keys(matchUpdates).length) return
    setMatches((prev) => {
      let changed = false
      const next = prev.map((m) => {
        const u = matchUpdates[m.id]
        if (!u) return m
        changed = true
        const mergedOdds = Array.isArray(u.odds) && u.odds.length
          ? u.odds
          : m.odds
        return {
          ...m,
          home_score: u.home_score ?? m.home_score,
          away_score: u.away_score ?? m.away_score,
          clock: u.clock ?? m.clock,
          period: u.period ?? m.period,
          status: u.status || m.status,
          odds: mergedOdds,
        }
      })
      if (changed) setLastUpdated(new Date())
      return changed ? sortMatchesByDuration(next) : prev
    })
  }, [matchUpdates])

  // 滚球采集交给后端 live_poller；仅在 WS 断线时偶尔触发 syncLive
  usePagePoll(
    async () => {
      try {
        if (!connected) {
          await bookmakersAPI.syncLive()
        }
        await loadMatches({ silent: true })
      } catch {
        /* ignore */
      }
    },
    connected ? 15000 : 45000,
  )

  const handleSearch = async () => {
    setLoading(true)
    try {
      const res = await matchesAPI.search(searchInput)
      setSearch(searchInput)
      let items = res.data || []
      if (site) items = items.filter((m) => (m.site_code || '').toLowerCase() === site)
      setMatches(sortMatchesByDuration(items))
    } catch (err) {
      toast.error('搜索失败')
    } finally {
      setLoading(false)
    }
  }

  const handleMatchClick = (matchId) => {
    subscribe(matchId)
    navigate(`/matches/${matchId}`)
  }

  return (
    <div className="page">
      <PageHeader
        eyebrow="赛事"
        title="滚球赛事"
        description="按站点和球类筛选"
        actions={(
          <div className="relative">
            <Search size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-ink-400" />
            <input
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              placeholder="搜索球队 / 联赛"
              className="input pl-9 w-64"
            />
          </div>
        )}
      >
        <p className="text-xs text-ink-400 mt-2">
          {connected ? (
            <span className="text-brand-700">实时已连接</span>
          ) : (
            <span className="text-amber-700">实时重连中</span>
          )}
          {lastUpdated ? (
            <span className="ml-2 text-ink-400">
              {refreshing ? '同步中…' : `更新于 ${lastUpdated.toLocaleTimeString('zh-CN')}`}
            </span>
          ) : null}
        </p>
      </PageHeader>

      <div className="flex gap-2 mb-3 overflow-x-auto pb-1">
        {SITE_TABS.map((s) => (
          <button
            key={s.value}
            type="button"
            onClick={() => setSite(s.value)}
            className={site === s.value ? 'chip-active' : 'chip-idle'}
          >
            {s.label}
          </button>
        ))}
      </div>

      <div className="flex gap-2 mb-5 overflow-x-auto pb-1">
        {SPORTS.map((s) => (
          <button
            key={s.value}
            type="button"
            onClick={() => setSport(s.value)}
            className={sport === s.value ? 'chip-active' : 'chip-idle'}
          >
            {s.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="flex items-center justify-center py-20">
          <div className="animate-spin w-7 h-7 border-2 border-ink-300 border-t-ink-900 rounded-full" />
        </div>
      ) : matches.length === 0 ? (
        <div className="card text-center py-16 text-ink-400 text-sm">暂无进行中赛事</div>
      ) : (
        <div className="grid gap-3">
          {matches.map((m) => (
            <MatchCard
              key={m.id}
              match={m}
              onClick={() => handleMatchClick(m.id)}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// === 赛事卡片 ===
function MatchCard({ match, onClick }) {
  const { oddsUpdates } = useWebSocket()
  const liveOdds = oddsUpdates[match.id]

  const isLive = match.status === 'live'

  return (
    <div
      onClick={onClick}
      className="card-hover cursor-pointer group"
    >
      <div className="flex items-center justify-between">
        {/* 左侧: 赛事信息 */}
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2.5">
            {isLive ? (
              <span className="badge-live">LIVE</span>
            ) : (
              <span className="badge-upcoming">{match.league}</span>
            )}
            {match.site_name || match.site_code ? (
              <span className="text-[11px] font-semibold px-1.5 py-0.5 rounded bg-ink-100 text-ink-700">
                {match.site_name || SITE_NAMES[match.site_code] || match.site_code}
              </span>
            ) : null}
            {isLive && (match.period || match.clock) ? (
              <span className="text-xs font-semibold text-rose-600 tabular-nums">
                {[match.period, match.clock].filter(Boolean).join(' · ')}
              </span>
            ) : match.start_time ? (
              <span className="text-xs text-ink-500 tabular-nums">
                {new Date(match.start_time).toLocaleString('zh-CN', {
                  month: 'numeric',
                  day: 'numeric',
                  hour: '2-digit',
                  minute: '2-digit',
                })}
              </span>
            ) : null}
            <span className="text-xs text-ink-500">{match.league}</span>
            <span className="text-xs text-ink-400">· {match.sport === 'football' || match.sport === 'soccer' ? '足球' : match.sport === 'basketball' ? '篮球' : '未知球类'}</span>
          </div>

          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 min-w-[120px]">
              <span className="font-semibold text-ink-900 group-hover:text-brand-700 transition-colors">
                {match.home_team}
              </span>
              {isLive && (
                <span className={`font-semibold text-lg tabular-nums ${liveOdds ? 'odds-flash-up' : ''}`}>
                  {Number(match.home_score ?? 0)}
                </span>
              )}
            </div>

            <span className="text-ink-300 text-xs font-semibold tracking-wide">
              {isLive ? `${Number(match.home_score ?? 0)} : ${Number(match.away_score ?? 0)}` : 'VS'}
            </span>

            <div className="flex items-center gap-2 min-w-[120px]">
              <span className="font-semibold text-ink-900 group-hover:text-brand-700 transition-colors">
                {match.away_team}
              </span>
              {isLive && (
                <span className={`font-semibold text-lg tabular-nums ${liveOdds ? 'odds-flash-down' : ''}`}>
                  {Number(match.away_score ?? 0)}
                </span>
              )}
            </div>
          </div>
        </div>

        <div className="flex items-center gap-4">
          {Array.isArray(match.odds) && match.odds.length > 0 && (
            <div className="flex gap-2">
              {(() => {
                const total = match.odds.find(o => o.bet_type === 'total' || o.bet_type === 'Total')
                if (!total || (total.odds_data?.under == null && total.odds_data?.over == null)) return null
                return (
                  <>
                    {total.odds_data?.under != null && (
                      <OddsChip label={`小球 ${total.total ?? ''}`} value={total.odds_data.under} />
                    )}
                    {total.odds_data?.over != null && (
                      <OddsChip label={`大球 ${total.total ?? ''}`} value={total.odds_data.over} />
                    )}
                  </>
                )
              })()}
            </div>
          )}
          <ChevronRight size={18} className="text-ink-300 group-hover:text-ink-700" />
        </div>
      </div>
    </div>
  )
}

function OddsChip({ label, value }) {
  const [prevValue, setPrevValue] = useState(value)
  const [flash, setFlash] = useState('')

  useEffect(() => {
    if (prevValue && value && value !== prevValue) {
      setFlash(value > prevValue ? 'odds-flash-up' : 'odds-flash-down')
      const timer = setTimeout(() => setFlash(''), 600)
      setPrevValue(value)
      return () => clearTimeout(timer)
    }
  }, [value])

  if (!value) return null

  return (
    <div className={`flex flex-col items-center px-3 py-1.5 rounded-lg bg-ink-50 border border-ink-100 ${flash}`}>
      <span className="text-[10px] text-ink-400 font-semibold">{label}</span>
      <span className="font-semibold text-sm tabular-nums text-ink-900">{Number(value).toFixed(2)}</span>
    </div>
  )
}
