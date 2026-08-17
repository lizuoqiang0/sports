import { useEffect, useRef, useState } from 'react'
import { bookmakersAPI } from '../lib/api.js'
import { extractErrorMessage } from '../lib/httpError.js'
import { BOOKMAKER_SITE_ORDER as SITE_ORDER } from '../lib/sites.js'
import { formatMoney } from '../lib/format.js'
import { usePagePoll } from '../hooks/usePagePoll.js'
import PageHeader from '../components/PageHeader.jsx'
import toast from 'react-hot-toast'
import {
  Globe, Link2, Loader2, RefreshCw, Save, ShieldCheck, Unplug, Zap
} from 'lucide-react'

const STATUS_STYLE = {
  connected: 'bg-brand-50 text-emerald-700 border-brand-200',
  disconnected: 'bg-ink-100 text-ink-600 border-ink-200',
  error: 'bg-red-50 text-red-600 border-red-200',
}

const STATUS_LABEL = {
  connected: '已连接',
  disconnected: '未连接',
  error: '异常',
}

const GATE_CODES = SITE_ORDER

export default function BookmakersPage() {
  const [accounts, setAccounts] = useState([])
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [busyCodes, setBusyCodes] = useState(() => new Set())
  const [verifyingAll, setVerifyingAll] = useState(false)
  const [syncing, setSyncing] = useState(false)
  const [gateOk, setGateOk] = useState(null)
  const savePromiseRef = useRef(null)
  const busyCodesRef = useRef(new Set())
  const accountsRef = useRef(accounts)
  accountsRef.current = accounts

  const markBusy = (code, on) => {
    setBusyCodes((prev) => {
      const next = new Set(prev)
      if (on) next.add(code)
      else next.delete(code)
      busyCodesRef.current = next
      return next
    })
  }

  const checkGate = async () => {
    try {
      const res = await bookmakersAPI.gateHealth()
      const ok = !!(res.success && res.data?.ok)
      setGateOk(ok)
      return ok
    } catch {
      setGateOk(false)
      return false
    }
  }

  useEffect(() => {
    load()
    checkGate()
  }, [])

  usePagePoll(checkGate, 15000)

  const load = async () => {
    setLoading(true)
    try {
      const res = await bookmakersAPI.list()
      setAccounts((res.data || [])
        .filter((a) => SITE_ORDER.includes(a.code))
        .map((a) => ({
          ...a,
          password: '',
          session_token: '',
        })))
    } catch (e) {
      toast.error(extractErrorMessage(e, '站点加载失败，请重试'))
    } finally {
      setLoading(false)
    }
  }

  const updateField = (code, field, value) => {
    setAccounts((prev) =>
      prev.map((a) => (a.code === code ? { ...a, [field]: value } : a))
    )
  }

  const buildPayload = () =>
    accountsRef.current.map((a) => ({
      code: a.code,
      base_url: a.base_url || '',
      username: a.username || '',
      password: a.password || undefined,
      session_token: a.session_token || undefined,
      enabled: a.enabled !== false,
    }))

  const ensureSaved = async () => {
    if (savePromiseRef.current) return savePromiseRef.current
    savePromiseRef.current = bookmakersAPI
      .save(buildPayload())
      .then((res) => {
        setAccounts((res.data || [])
          .filter((a) => SITE_ORDER.includes(a.code))
          .map((a) => ({
            ...a,
            password: '',
            session_token: '',
          })))
        return res
      })
      .finally(() => {
        savePromiseRef.current = null
      })
    return savePromiseRef.current
  }

  const applyAccountPatch = (account) => {
    if (!account?.code) return
    setAccounts((prev) =>
      prev.map((a) =>
        a.code === account.code
          ? { ...a, ...account, password: '', session_token: '' }
          : a
      )
    )
  }

  const handleSave = async () => {
    setSaving(true)
    try {
      await ensureSaved()
      toast.success('站点已保存')
    } catch (e) {
      toast.error(e.message || e.detail || '保存失败')
    } finally {
      setSaving(false)
    }
  }

  const handleVerify = async (code) => {
    if (busyCodesRef.current.has(code)) return
    markBusy(code, true)
    const toastId = `bm-verify-${code}`
    try {
      const ok = await checkGate()
      const acc = accountsRef.current.find((a) => a.code === code)
      const needsGate = GATE_CODES.includes(code)
      if (needsGate && !ok && !(acc?.has_session_token || acc?.session_token)) {
        toast.error('Browser Gate 未连接，请先启动', { id: toastId })
        return
      }
      if (acc?.base_url) {
        toast.loading(
          ok
            ? `正在验证 ${acc.name || code}…`
            : `正在使用已保存会话验证 ${acc.name || code}…`,
          { id: toastId },
        )
      }
      await ensureSaved()
      const res = await bookmakersAPI.verify(code)
      toast.success(res.message || `${code} 验证成功`, { id: toastId })
      if (res.data?.account) applyAccountPatch(res.data.account)
      else await load()
      await checkGate()
    } catch (e) {
      const msg = e.message || e.detail || (e.code === 'ECONNABORTED' ? '验证超时，请重试' : '验证失败')
      toast.error(typeof msg === 'string' ? msg : '验证失败', { id: toastId })
      await load()
      await checkGate()
    } finally {
      markBusy(code, false)
    }
  }

  const handleVerifyAll = async () => {
    if (verifyingAll) return
    const targets = accountsRef.current.filter(
      (a) => (a.base_url || '').trim() && (a.username || '').trim()
    )
    if (!targets.length) {
      toast.error('请先填写站点网址和账号')
      return
    }
    setVerifyingAll(true)
    targets.forEach((a) => markBusy(a.code, true))
    const toastId = 'bm-verify-all'
    try {
      const ok = await checkGate()
      const needGate = targets.some((a) => GATE_CODES.includes(a.code))
      if (needGate && !ok) {
        const hasToken = targets.some(
          (a) => GATE_CODES.includes(a.code) && (a.has_session_token || a.session_token)
        )
        if (!hasToken) {
          toast.error('Browser Gate 未连接，请先启动', { id: toastId })
          return
        }
      }
      toast.loading(
        `正在验证 ${targets.length} 个站点…`,
        { id: toastId },
      )
      await ensureSaved()
      const res = await bookmakersAPI.verifyBatch(targets.map((a) => a.code))
      const results = res.data?.results || []
      results.forEach((r) => {
        if (r.account) applyAccountPatch(r.account)
        const tid = `bm-verify-${r.code}`
        if (r.ok) toast.success(r.message || `${r.code} 成功`, { id: tid })
        else toast.error(r.message || `${r.code} 失败`, { id: tid })
      })
      toast.success(res.message || '验证完成', { id: toastId })
      await load()
      await checkGate()
    } catch (e) {
      const msg = e.message || e.detail || (e.code === 'ECONNABORTED' ? '验证超时' : '批量验证失败')
      toast.error(typeof msg === 'string' ? msg : '批量验证失败', { id: toastId })
      await load()
    } finally {
      targets.forEach((a) => markBusy(a.code, false))
      setVerifyingAll(false)
    }
  }

  const handleDisconnect = async (code) => {
    if (busyCodesRef.current.has(code)) return
    markBusy(code, true)
    try {
      const res = await bookmakersAPI.disconnect(code)
      toast.success(res.message || '已断开')
      await load()
      await checkGate()
    } catch (e) {
      toast.error(e.message || e.detail || '断开失败')
      await load()
    } finally {
      markBusy(code, false)
    }
  }

  const handleSync = async () => {
    setSyncing(true)
    const toastId = toast.loading('正在同步滚球…')
    try {
      const res = await bookmakersAPI.syncLive()
      toast.success(res.message || '滚球已刷新', { id: toastId })
      await load()
    } catch (e) {
      const fallback = e?.code === 'ECONNABORTED'
        ? '同步超时，请确认 Browser Gate 已启动'
        : '同步失败，请稍后重试'
      toast.error(extractErrorMessage(e, fallback), { id: toastId })
    } finally {
      setSyncing(false)
    }
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center h-64">
        <Loader2 className="animate-spin text-brand-600" size={28} />
      </div>
    )
  }

  const anyBusy = busyCodes.size > 0 || verifyingAll

  return (
    <div className="page">
        <PageHeader
        eyebrow="接入"
        title="站点"
        description="填写真实网址、账号并验证"
        actions={(
          <>
            <button
              onClick={handleVerifyAll}
              disabled={anyBusy || syncing}
              className="btn-outline"
              title="验证全部已填写站点"
            >
              {verifyingAll ? <Loader2 size={16} className="animate-spin" /> : <Zap size={16} />}
              验证全部
            </button>
            <button onClick={handleSync} disabled={syncing || verifyingAll} className="btn-outline">
              {syncing ? <Loader2 size={16} className="animate-spin" /> : <RefreshCw size={16} />}
              同步滚球
            </button>
            <button onClick={handleSave} disabled={saving} className="btn-primary">
              {saving ? <Loader2 size={16} className="animate-spin" /> : <Save size={16} />}
              保存
            </button>
          </>
        )}
      >
        {gateOk === true && (
          <p className="text-xs text-brand-700 mt-2">Browser Gate 已连接</p>
        )}
        {gateOk === false && (
          <p className="text-xs text-rose-600 mt-2">
            Browser Gate 未连接，请先启动
          </p>
        )}
      </PageHeader>

      <div className="grid md:grid-cols-2 gap-4">
        {accounts.map((acc) => {
          const isLive = acc.mode === 'live'
          const isBusy = busyCodes.has(acc.code)
          return (
            <div key={acc.code} className="card">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-3">
                  <div className="w-10 h-10 rounded-xl bg-ink-900 text-white flex items-center justify-center">
                    <Globe size={16} />
                  </div>
                  <div>
                    <div className="font-semibold text-ink-900 flex items-center gap-2">
                      {acc.name}
                      <span className={`badge border ${
                        isLive
                          ? 'bg-brand-50 text-brand-700 border-brand-200'
                          : 'bg-amber-50 text-amber-800 border-amber-200'
                      }`}>
                        {isLive ? '真实站点' : '待填写'}
                      </span>
                    </div>
                    <div className="text-xs text-ink-400 uppercase tracking-wide">{acc.code}</div>
                  </div>
                </div>
                <span className={`badge border ${STATUS_STYLE[acc.status] || STATUS_STYLE.disconnected}`}>
                  {STATUS_LABEL[acc.status] || acc.status}
                </span>
              </div>

              <div className="space-y-3">
                <label className="block text-sm">
                  <span className="text-ink-500 text-xs font-semibold">网址</span>
                  <input
                    className="input mt-1"
                    value={acc.base_url || ''}
                    onChange={(e) => updateField(acc.code, 'base_url', e.target.value)}
                    placeholder="https://真实站点"
                  />
                </label>
                <label className="block text-sm">
                  <span className="text-ink-500 text-xs font-semibold">账号</span>
                  <input
                    className="input mt-1"
                    value={acc.username || ''}
                    onChange={(e) => updateField(acc.code, 'username', e.target.value)}
                    placeholder="登录用户名"
                    autoComplete="off"
                  />
                </label>
                <label className="block text-sm">
                  <span className="text-ink-500 text-xs font-semibold">
                    密码 {acc.has_password ? '（已保存，留空则不修改）' : ''}
                  </span>
                  <input
                    type="password"
                    className="input mt-1"
                    value={acc.password || ''}
                    onChange={(e) => updateField(acc.code, 'password', e.target.value)}
                    placeholder={acc.has_password ? '••••••••' : '登录密码'}
                    autoComplete="new-password"
                  />
                </label>

                {GATE_CODES.includes(acc.code) && (
                  <>
                    <p className="panel-note mt-1.5">
                      验证时会拉起浏览器，请进入目标盘口并保持会话。
                    </p>
                    <label className="block text-sm">
                      <span className="text-ink-500 text-xs font-semibold">
                        会话 / Token（可选）
                        {acc.has_session_token ? ' · 已保存' : ''}
                      </span>
                      <input
                        className="input mt-1 font-mono text-xs"
                        value={acc.session_token || ''}
                        onChange={(e) => updateField(acc.code, 'session_token', e.target.value)}
                        placeholder={
                          acc.has_session_token
                            ? '已保存，留空不修改'
                            : (acc.code === 'ob' ? '粘贴 X-API-TOKEN' : '可选：粘贴会话 Token')
                        }
                        autoComplete="off"
                      />
                    </label>
                  </>
                )}
              </div>

              {acc.profile && (acc.profile.name || acc.profile.member_id) && (
                <div className="mt-3 rounded-xl bg-ink-50 border border-ink-100 px-3 py-2 text-xs text-ink-600">
                  <div className="font-semibold text-ink-800 mb-0.5">账号信息</div>
                  <div className="flex flex-wrap gap-x-3 gap-y-1">
                    {acc.profile.name && <span>昵称 {acc.profile.name}</span>}
                    {acc.profile.member_id && <span>ID {acc.profile.member_id}</span>}
                  </div>
                </div>
              )}

              <div className="mt-3 text-xs text-ink-400">
                最近同步：{acc.last_sync_at ? new Date(acc.last_sync_at).toLocaleString() : '暂无'}
              </div>

              <div className="mt-4 flex items-center justify-between gap-2">
                <div className="text-sm text-ink-500">
                  余额 <span className="font-semibold text-ink-900 tabular-nums">¥{formatMoney(acc.balance)}</span>
                </div>
                <div className="flex items-center gap-2">
                  {(acc.status === 'connected' || acc.has_session_token) && (
                    <button
                      onClick={() => handleDisconnect(acc.code)}
                      disabled={isBusy}
                      className="btn-outline flex items-center gap-1.5 text-sm"
                      title="断开会话"
                    >
                      {isBusy ? (
                        <Loader2 size={14} className="animate-spin" />
                      ) : (
                        <Unplug size={14} />
                      )}
                      断开
                    </button>
                  )}
                  <button
                    onClick={() => handleVerify(acc.code)}
                    disabled={isBusy}
                    className="btn-primary flex items-center gap-2 text-sm"
                  >
                    {isBusy ? (
                      <Loader2 size={14} className="animate-spin" />
                    ) : acc.status === 'connected' ? (
                      <ShieldCheck size={14} />
                    ) : (
                      <Link2 size={14} />
                    )}
                    {acc.status === 'connected' ? '重新验证' : '验证'}
                  </button>
                </div>
              </div>

              {acc.last_error && (
                <div className="mt-3 text-xs text-red-600 bg-red-50 border border-red-100 rounded-lg px-3 py-2 flex gap-2">
                  <Unplug size={14} className="shrink-0 mt-0.5" />
                  {acc.last_error}
                </div>
              )}
            </div>
          )
        })}
      </div>
    </div>
  )
}
