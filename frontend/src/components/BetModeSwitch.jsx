import { useEffect, useState } from 'react'
import { Hand, Zap, Loader2 } from 'lucide-react'
import toast from 'react-hot-toast'
import { monitoringAPI } from '../lib/api.js'

/**
 * 全局下单模式开关：人工 / 自动
 * - 人工：仅生成机会/推荐，需手动确认后真实下单
 * - 自动：扫描通过后自动真实下单
 */
export default function BetModeSwitch({ className = '', onChange }) {
  const [betMode, setBetMode] = useState(null)
  const [meta, setMeta] = useState(null)
  const [loading, setLoading] = useState(true)
  const [switching, setSwitching] = useState(false)

  const load = async () => {
    try {
      const res = await monitoringAPI.getBetMode()
      setBetMode(res.data?.bet_mode || 'manual')
      setMeta(res.data || null)
      onChange?.(res.data)
    } catch {
      /* ignore */
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const toggle = async (next) => {
    if (switching || loading || next === betMode) return
    setSwitching(true)
    try {
      const res = await monitoringAPI.setBetMode(next)
      setBetMode(res.data?.bet_mode || next)
      setMeta(res.data || null)
      onChange?.(res.data)
      toast.success(res.message || (next === 'active' ? '已切换为自动模式' : '已切换为人工模式'))
    } catch (err) {
      toast.error(err?.detail || '切换失败')
    } finally {
      setSwitching(false)
    }
  }

  const isActive = betMode === 'active'
  const disabled = switching || loading || betMode === null

  return (
    <div className={`flex flex-col gap-1 ${className}`}>
      <div className="flex items-center gap-1 p-1 rounded-2xl border border-ink-200 bg-white shadow-sm">
        <button
          type="button"
          disabled={disabled}
          onClick={() => toggle('manual')}
          className={`flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-semibold transition-all ${
            betMode === null
              ? 'text-ink-400'
              : (!isActive ? 'bg-ink-900 text-white' : 'text-ink-500 hover:text-ink-800')
          }`}
        >
          {loading ? <Loader2 size={15} className="animate-spin" /> : <Hand size={15} />}
          人工
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => toggle('active')}
          className={`flex items-center gap-1.5 px-3 py-2 rounded-xl text-sm font-semibold transition-all ${
            betMode === null
              ? 'text-ink-400'
              : (isActive ? 'bg-brand-700 text-white' : 'text-ink-500 hover:text-ink-800')
          }`}
        >
          {switching ? <Loader2 size={15} className="animate-spin" /> : <Zap size={15} />}
          自动
        </button>
      </div>
      {meta?.description && (
        <p className="text-xs text-ink-500 px-1 max-w-xs">{meta.description}</p>
      )}
    </div>
  )
}
