import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../store/auth.jsx'
import { authAPI } from '../lib/api.js'
import toast from 'react-hot-toast'
import { Eye, EyeOff, Loader2 } from 'lucide-react'

export default function LoginPage() {
  const [isLogin, setIsLogin] = useState(true)
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)
  const [form, setForm] = useState({
    username: '',
    email: '',
    password: '',
  })

  const { login } = useAuth()
  const navigate = useNavigate()

  const handleSubmit = async (e) => {
    e.preventDefault()
    setLoading(true)

    try {
      if (isLogin) {
        await login(form.username, form.password)
        toast.success('登录成功')
      } else {
        await authAPI.register(form)
        await login(form.username, form.password)
        toast.success('注册成功')
      }
      navigate('/')
    } catch (err) {
      const msg = err?.detail || err?.message || (typeof err === 'string' ? err : '操作失败')
      toast.error(Array.isArray(msg) ? msg[0]?.msg || '操作失败' : msg)
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-center">
        <header className="login-brand">
          <p className="login-mark">OB Sports</p>
          <h1 className="login-title">{isLogin ? '登录' : '注册'}</h1>
          <p className="login-subtitle">
            {isLogin
              ? '登录后继续使用系统'
              : '注册后即可进入系统'}
          </p>
        </header>

        <section className="login-panel">
          <div className="login-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={isLogin}
              onClick={() => setIsLogin(true)}
              className={`login-tab ${isLogin ? 'is-active' : ''}`}
            >
              登录
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={!isLogin}
              onClick={() => setIsLogin(false)}
              className={`login-tab ${!isLogin ? 'is-active' : ''}`}
            >
              注册
            </button>
          </div>

          <form onSubmit={handleSubmit} className="login-form">
            <label className="login-field">
              <span>{isLogin ? '用户名或邮箱' : '用户名'}</span>
              <input
                type="text"
                value={form.username}
                onChange={(e) => setForm({ ...form, username: e.target.value })}
                placeholder={isLogin ? '请输入用户名或邮箱' : '3–50 个字符'}
                autoComplete="username"
                required
              />
            </label>

            {!isLogin && (
              <label className="login-field">
                <span>邮箱</span>
                <input
                  type="email"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  placeholder="name@example.com"
                  autoComplete="email"
                  required
                />
              </label>
            )}

            <label className="login-field">
              <span>密码</span>
              <div className="login-password">
                <input
                  type={showPassword ? 'text' : 'password'}
                  value={form.password}
                  onChange={(e) => setForm({ ...form, password: e.target.value })}
                  placeholder="至少 8 位密码"
                  minLength={8}
                  autoComplete={isLogin ? 'current-password' : 'new-password'}
                  required
                />
                <button
                  type="button"
                  className="login-eye"
                  onClick={() => setShowPassword(!showPassword)}
                  aria-label={showPassword ? '隐藏密码' : '显示密码'}
                >
                  {showPassword ? <EyeOff size={18} /> : <Eye size={18} />}
                </button>
              </div>
            </label>

            <button type="submit" disabled={loading} className="login-submit">
              {loading && <Loader2 size={18} className="animate-spin" />}
              {isLogin ? '进入工作台' : '注册并登录'}
            </button>
          </form>

          <p className="login-hint">
            登录后先到「站点」完成验证
          </p>
        </section>

        <p className="login-legal">仅支持真实站点</p>
      </div>
    </div>
  )
}
