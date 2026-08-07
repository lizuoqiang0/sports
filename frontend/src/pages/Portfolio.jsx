import { useState, useEffect } from 'react'
import { betsAPI } from '../lib/api.js'
import PageHeader from '../components/PageHeader.jsx'
import toast from 'react-hot-toast'
import { Loader2, ChevronLeft, ChevronRight } from 'lucide-react'
import { format } from 'date-fns'

const STATUS_MAP = {
  success: { label: '成功', color: 'text-brand-700 bg-brand-50' },
  failed: { label: '失败', color: 'text-red-600 bg-red-50' },
}

const SITE_LABELS = {
  ob: 'OB',
  pinnacle: '平博',
}

export default function PortfolioPage() {
  const [portfolio, setPortfolio] = useState(null)
  const [bets, setBets] = useState([])
  const [loading, setLoading] = useState(true)
  const [page, setPage] = useState(1)
  const [total, setTotal] = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const [siteFilter, setSiteFilter] = useState('')
  const pageSize = 20

  useEffect(() => {
    loadData()
  }, [page, statusFilter, siteFilter])

  const loadData = async () => {
    setLoading(true)
    try {
      const [portRes, betsRes] = await Promise.all([
        betsAPI.portfolio(),
        betsAPI.history({
          page,
          page_size: pageSize,
          status: statusFilter || undefined,
          provider: siteFilter || undefined,
        }),
      ])
      setPortfolio(portRes.data)
      setBets(betsRes.data?.items || [])
      setTotal(betsRes.data?.total || 0)
    } catch (err) {
      toast.error('加载失败')
    } finally {
      setLoading(false)
    }
  }

  const totalPages = Math.ceil(total / pageSize)

  return (
    <div className="page">
      <PageHeader
        eyebrow="持仓"
        title="投注记录"
        description="AI 自动投注记录"
      />

      {/* 今日投注 / 总投注 */}
      {portfolio && (
        <div className="card mb-6">
          <div className="flex items-center justify-around">
            <div className="text-center">
              <div className="text-sm text-gray-400">今日投注</div>
              <div className="text-2xl font-bold text-brand-700 mt-1">{portfolio.today_bets} 笔</div>
              <div className="text-xs text-ink-400 mt-0.5">每日 0 点清零</div>
            </div>
            <div className="w-px h-12 bg-ink-100" />
            <div className="text-center">
              <div className="text-sm text-gray-400">总投注</div>
              <div className="text-2xl font-bold mt-1">{portfolio.total_bets} 笔</div>
              <div className="text-xs text-ink-400 mt-0.5">每月末清零</div>
            </div>
          </div>
        </div>
      )}

      {/* 投注列表 */}
      <div className="card">
        <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
          <h3 className="section-title">投注记录</h3>
          <div className="flex flex-wrap items-center gap-2">
            {['', 'ob', 'pinnacle'].map((s) => (
              <button
                key={s || 'site-all'}
                type="button"
                onClick={() => { setSiteFilter(s); setPage(1) }}
                className={`px-3 py-1.5 text-xs rounded-lg font-medium ${
                  siteFilter === s
                    ? 'bg-brand-600 text-white'
                    : 'bg-ink-50 text-ink-500 hover:text-ink-900'
                }`}
              >
                {s === '' ? '全部站点' : SITE_LABELS[s] || s}
              </button>
            ))}
            {['', 'success', 'failed'].map((s) => (
              <button
                key={s || 'all'}
                type="button"
                onClick={() => { setStatusFilter(s); setPage(1) }}
                className={`px-3 py-1.5 text-xs rounded-lg font-medium ${
                  statusFilter === s
                    ? 'bg-ink-900 text-white'
                    : 'bg-ink-50 text-ink-500 hover:text-ink-900'
                }`}
              >
                {s === '' ? '全部' : (STATUS_MAP[s]?.label || s)}
              </button>
            ))}
          </div>
        </div>

        {loading ? (
          <div className="flex justify-center py-10">
            <Loader2 size={24} className="animate-spin text-brand-500" />
          </div>
        ) : bets.length === 0 ? (
          <div className="text-center py-10 text-gray-500">
            暂无投注记录
          </div>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="text-gray-500 text-xs border-b border-gray-200">
                    <th className="text-left py-2 px-3">投注比赛</th>
                    <th className="text-left py-2 px-3">时间</th>
                    <th className="text-center py-2 px-3">状态</th>
                    <th className="text-center py-2 px-3">站点</th>
                  </tr>
                </thead>
                <tbody>
                  {bets.map((bet) => {
                    const status = STATUS_MAP[bet.status] || { label: bet.status, color: '' }
                    const siteName = SITE_LABELS[bet.provider_code] || bet.provider || '-'
                    return (
                      <tr key={bet.id} className="border-b border-gray-100 hover:bg-gray-50">
                        <td className="py-2.5 px-3 max-w-[280px] truncate" title={bet.match_info}>
                          {bet.match_info}
                        </td>
                        <td className="py-2.5 px-3 text-gray-400 whitespace-nowrap">
                          {bet.created_at ? format(new Date(bet.created_at), 'MM-dd HH:mm') : '-'}
                        </td>
                        <td className="py-2.5 px-3 text-center">
                          <span className={`px-2 py-0.5 rounded-full text-[10px] ${status.color}`}>
                            {status.label}
                          </span>
                        </td>
                        <td className="py-2.5 px-3 text-center">
                          <span className="text-xs text-ink-600">{siteName}</span>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>

            {totalPages > 1 && (
              <div className="flex items-center justify-between mt-4">
                <span className="text-xs text-gray-500">
                  共 {total} 条 · 第 {page}/{totalPages} 页
                </span>
                <div className="flex gap-2">
                  <button
                    onClick={() => setPage(p => Math.max(1, p - 1))}
                    disabled={page === 1}
                    className="btn-outline py-1 px-3"
                  >
                    <ChevronLeft size={14} />
                  </button>
                  <button
                    onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                    disabled={page === totalPages}
                    className="btn-outline py-1 px-3"
                  >
                    <ChevronRight size={14} />
                  </button>
                </div>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  )
}
