import { NavLink, Outlet, useNavigate, useLocation } from 'react-router-dom'
import { useAuth } from '../store/auth.jsx'
import { useWebSocket } from '../store/websocket.jsx'
import { aiAPI } from '../lib/api.js'
import { pushLogOnce } from '../store/aiLogs.jsx'
import {
  LayoutDashboard, Trophy, Wallet,
  Bot, LogOut, Wifi, WifiOff, User, Globe, Menu, X, ScrollText
} from 'lucide-react'
import { useEffect, useState } from 'react'
import toast from 'react-hot-toast'
import { formatMoney } from '../lib/format.js'

const primaryNav = [
  { to: '/', icon: LayoutDashboard, label: '工作台', end: true },
  { to: '/matches', icon: Trophy, label: '赛事' },
  { to: '/portfolio', icon: Wallet, label: '持仓' },
]

const intelligenceNav = [
  { to: '/ai', icon: Bot, label: 'AI 投注' },
  { to: '/bookmakers', icon: Globe, label: '站点' },
  { to: '/logs', icon: ScrollText, label: '日志' },
]

function NavGroup({ title, items, onNavigate }) {
  return (
    <div className="mb-7">
      <div className="px-3 mb-2.5 text-[10px] font-semibold tracking-[0.16em] uppercase text-white/30">
        {title}
      </div>
      <div className="space-y-0.5">
        {items.map((item) => (
          <NavLink
            key={item.to}
            to={item.to}
            end={item.end}
            onClick={onNavigate}
            className={({ isActive }) =>
              `nav-link ${isActive ? 'is-active' : ''}`
            }
          >
            <item.icon size={17} strokeWidth={1.75} />
            <span className="font-medium">{item.label}</span>
          </NavLink>
        ))}
      </div>
    </div>
  )
}

export default function Layout() {
  const { user, logout } = useAuth()
  const { connected } = useWebSocket()
  const navigate = useNavigate()
  const location = useLocation()
  const [showUserMenu, setShowUserMenu] = useState(false)
  const [navOpen, setNavOpen] = useState(false)
  const [aiRuntime, setAiRuntime] = useState(null)

  useEffect(() => {
    setNavOpen(false)
    setShowUserMenu(false)
  }, [location.pathname])

  useEffect(() => {
    let alive = true
    const loadAiRuntime = async () => {
      if (!user) {
        if (alive) setAiRuntime(null)
        return
      }
      try {
        const res = await aiAPI.status()
        if (alive) {
          const runtime = res.data || null
          setAiRuntime(runtime)
          if (runtime?.effective_label) {
            pushLogOnce(
              `runtime:${runtime.effective_state || 'unknown'}:${runtime.engine_running ? '1' : '0'}:${runtime.execution_mode || runtime.bet_mode || ''}`,
              'engine',
              `${runtime.effective_label}${runtime.execution_mode_label ? ` · ${runtime.execution_mode_label}` : ''}`,
              runtime,
              12000,
            )
          }
        }
      } catch {
        if (alive) setAiRuntime(null)
      }
    }
    loadAiRuntime()
    return () => {
      alive = false
    }
  }, [user, location.pathname])

  useEffect(() => {
    if (!navOpen) return undefined
    const onKey = (e) => {
      if (e.key === 'Escape') setNavOpen(false)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [navOpen])

  const handleLogout = async () => {
    await logout()
    toast.success('已退出登录')
    navigate('/login')
  }

  const aiTone = aiRuntime?.badge_tone || 'slate'
  const aiLabel = aiRuntime?.effective_label || 'AI 未启动'
  const aiBadgeClass = (
    aiTone === 'green'
      ? 'bg-brand-50 text-brand-700'
      : aiTone === 'amber'
        ? 'bg-yellow-50 text-yellow-700'
        : aiTone === 'orange'
          ? 'bg-orange-50 text-orange-700'
          : 'bg-ink-100 text-ink-500'
  )

  return (
    <div className="app-shell text-ink-900">
      {navOpen ? (
        <button
          type="button"
          className="sidebar-scrim"
          aria-label="关闭导航"
          onClick={() => setNavOpen(false)}
        />
      ) : null}

      <aside className={`app-sidebar ${navOpen ? 'is-open' : ''}`}>
        <div className="app-sidebar-brand">
          <div className="app-mark">OB</div>
          <div className="min-w-0 flex-1">
            <div className="font-display text-[14px] text-white tracking-tight">OB Sports</div>
            <div className="text-[11px] text-white/40 mt-0.5">投注工作台</div>
          </div>
          <button
            type="button"
            className="md:hidden p-1.5 text-white/60 hover:text-white"
            onClick={() => setNavOpen(false)}
            aria-label="关闭菜单"
          >
            <X size={18} />
          </button>
        </div>

        <nav className="flex-1 py-6 px-3 overflow-y-auto">
          <NavGroup title="交易" items={primaryNav} onNavigate={() => setNavOpen(false)} />
          <NavGroup title="智能" items={intelligenceNav} onNavigate={() => setNavOpen(false)} />
        </nav>

        <div className="p-3 border-t border-white/[0.07]">
          <div className={`flex items-center gap-2 px-3 py-2 rounded-lg text-xs mb-2.5 font-medium ${
            connected
              ? 'text-[#d8f5a6] bg-[rgba(200,240,108,0.12)]'
              : 'text-rose-200 bg-rose-500/15'
          }`}>
            {connected ? <Wifi size={13} /> : <WifiOff size={13} />}
            {connected ? '实时已连接' : '实时已断开'}
          </div>

          <div className="relative">
            <button
              type="button"
              onClick={() => setShowUserMenu(!showUserMenu)}
              className="w-full flex items-center gap-3 px-3 py-2.5 rounded-lg hover:bg-white/[0.06] transition-colors"
            >
              <div className="w-9 h-9 rounded-lg bg-brand-600/90 text-white flex items-center justify-center shrink-0 ring-1 ring-white/10">
                <User size={15} />
              </div>
              <div className="flex-1 text-left min-w-0">
                <div className="text-sm font-semibold truncate text-white/95">{user?.username}</div>
                <div className="text-xs text-white/40 tabular-nums">
                  ¥{formatMoney(user?.balance)}
                </div>
              </div>
            </button>

            {showUserMenu && (
              <div className="absolute bottom-full left-0 right-0 mb-2 bg-white border border-ink-100 rounded-xl shadow-lift overflow-hidden z-20">
                <div className="px-4 py-3 border-b border-ink-100">
                  <div className="text-sm font-semibold text-ink-900">{user?.username}</div>
                  <div className="text-xs text-ink-500 mt-0.5 truncate">{user?.email}</div>
                  <div className="text-xs mt-2.5">
                    <span className={`badge ${aiBadgeClass}`}>
                      {aiLabel}
                    </span>
                  </div>
                </div>
                <button
                  type="button"
                  onClick={handleLogout}
                  className="w-full flex items-center gap-2 px-4 py-3 text-sm text-rose-600 hover:bg-rose-50 font-medium"
                >
                  <LogOut size={14} />
                  退出登录
                </button>
              </div>
            )}
          </div>
        </div>
      </aside>

      <main className="app-main">
        <div className="app-topbar">
          <button
            type="button"
            className="inline-flex items-center justify-center w-10 h-10 rounded-lg border border-ink-200 bg-white text-ink-800"
            onClick={() => setNavOpen(true)}
            aria-label="打开导航"
          >
            <Menu size={18} />
          </button>
          <div className="font-display text-[15px] tracking-tight text-ink-900">OB Sports</div>
          <div className={`w-2 h-2 rounded-sm ${connected ? 'bg-brand-500' : 'bg-rose-500'}`} title={connected ? '已连接' : '已断开'} />
        </div>
        <Outlet />
      </main>
    </div>
  )
}
