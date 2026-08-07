import { useState, Component } from 'react'
import { ScrollText, Trash2 } from 'lucide-react'
import PageHeader from '../components/PageHeader.jsx'
import { useAiLogs, clearLogs } from '../store/aiLogs.jsx'

class LogListErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false }
  }
  static getDerivedStateFromError() {
    return { hasError: true }
  }
  componentDidCatch(err) {
    console.error('Log render error:', err)
  }
  render() {
    if (this.state.hasError) {
      return (
        <div className="text-center py-8 text-ink-400">
          <p className="text-sm">部分日志渲染异常</p>
          <button
            onClick={() => { this.setState({ hasError: false }); clearLogs() }}
            className="mt-2 text-xs text-brand-600 hover:underline"
          >
            清空日志重试
          </button>
        </div>
      )
    }
    return this.props.children
  }
}

const LOG_TYPES = [
  { key: 'all', label: '全部' },
  { key: 'bet_placed', label: '下单成功' },
  { key: 'bet_failed', label: '下单失败' },
  { key: 'risk', label: '风控' },
  { key: 'cycle', label: '分析轮次' },
  { key: 'engine', label: '引擎' },
  { key: 'config', label: '配置' },
  { key: 'analysis', label: '分析' },
  { key: 'recommend', label: '推荐' },
  { key: 'prefetch', label: '数据源' },
]

const TYPE_META = {
  bet_placed: { icon: '✓', color: 'text-brand-700', bg: 'bg-brand-50/60 border-brand-100' },
  bet_failed: { icon: '✗', color: 'text-rose-600', bg: 'bg-rose-50/60 border-rose-100' },
  risk:       { icon: '⚠', color: 'text-amber-600', bg: 'bg-amber-50/60 border-amber-100' },
  cycle:      { icon: '⟳', color: 'text-sky-600', bg: 'bg-sky-50/60 border-sky-100' },
  engine:     { icon: '⚙', color: 'text-ink-500', bg: 'bg-ink-50/60 border-ink-100' },
  config:     { icon: '⚙', color: 'text-ink-500', bg: 'bg-ink-50/60 border-ink-100' },
  analysis:   { icon: '📊', color: 'text-ink-500', bg: 'bg-ink-50/60 border-ink-100' },
  recommend:   { icon: '📊', color: 'text-ink-500', bg: 'bg-ink-50/60 border-ink-100' },
  prefetch:   { icon: '📡', color: 'text-violet-600', bg: 'bg-violet-50/60 border-violet-100' },
}

export default function LogsPage() {
  const [filter, setFilter] = useState('all')
  const logs = useAiLogs()

  const filtered = filter === 'all' ? logs : logs.filter((l) => l.type === filter)

  const typeCounts = {}
  logs.forEach((l) => { typeCounts[l.type] = (typeCounts[l.type] || 0) + 1 })

  return (
    <>
      <PageHeader
        title="AI 日志"
        description="AI 分析结果 · 下单记录 · 风控触发"
      />

      {/* 筛选栏 */}
      <div className="flex items-center gap-1.5 mb-4 flex-wrap">
        {LOG_TYPES.map((t) => {
          const count = t.key === 'all' ? logs.length : (typeCounts[t.key] || 0)
          const active = filter === t.key
          return (
            <button
              key={t.key}
              onClick={() => setFilter(t.key)}
              className={`px-2.5 py-1 rounded-md text-xs font-medium transition-colors ${
                active
                  ? 'bg-brand-700 text-white'
                  : 'bg-ink-100 text-ink-500 hover:bg-ink-200'
              }`}
            >
              {t.label}
              {count > 0 && (
                <span className={`ml-1 ${active ? 'text-white/70' : 'text-ink-400'}`}>
                  {count}
                </span>
              )}
            </button>
          )
        })}

        <div className="flex-1" />

        {logs.length > 0 && (
          <button
            onClick={() => clearLogs()}
            className="flex items-center gap-1 px-2.5 py-1 rounded-md text-xs text-ink-400 hover:text-rose-500 hover:bg-rose-50 transition-colors"
          >
            <Trash2 size={13} />
            清空
          </button>
        )}
      </div>

      {/* 日志列表 */}
      <div className="card p-0 overflow-hidden">
        <LogListErrorBoundary>
        {filtered.length === 0 ? (
          <div className="text-center py-16 text-ink-400">
            <ScrollText size={40} className="mx-auto mb-3 opacity-30" />
            <p className="text-sm">暂无日志记录</p>
            <p className="text-xs mt-1">启动 AI 分析后将自动记录</p>
          </div>
        ) : (
          <div className="divide-y divide-ink-100">
            {filtered.map((log) => {
              const meta = TYPE_META[log.type] || TYPE_META.engine
              return (
                <div key={log.id} className={`flex items-start gap-3 px-4 py-3 border-l-3 ${meta.bg}`}>
                  <span className={`shrink-0 mt-0.5 text-sm ${meta.color}`}>
                    {meta.icon}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm text-ink-700 leading-snug break-words">
                      {log.message}
                    </div>
                    {log.detail && (
                      <div className="text-xs text-ink-400 mt-1 break-words">
                        {log.detail}
                      </div>
                    )}
                  </div>
                  <span className="shrink-0 text-[11px] text-ink-400 tabular-nums whitespace-nowrap">
                    {(() => {
                      try {
                        const d = new Date(log.time)
                        return isNaN(d.getTime()) ? '--:--:--' : d.toLocaleTimeString('zh-CN', { hour12: false })
                      } catch {
                        return '--:--:--'
                      }
                    })()}
                  </span>
                </div>
              )
            })}
          </div>
        )}
        </LogListErrorBoundary>
      </div>

      {/* 统计 */}
      {logs.length > 0 && (
        <div className="mt-3 text-xs text-ink-400 text-right">
          共 {logs.length} 条日志{filter !== 'all' && ` · 筛选后 ${filtered.length} 条`}
        </div>
      )}
    </>
  )
}
