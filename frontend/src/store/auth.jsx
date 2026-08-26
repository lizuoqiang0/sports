import { createContext, useCallback, useContext, useState, useEffect } from 'react'
import { authAPI, clearStoredSession, refreshAccessToken } from '../lib/api.js'

const AuthContext = createContext(null)

function normalizeUser(userData) {
  return {
    ...userData,
    balance: Number(userData?.balance ?? 0),
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [token, setToken] = useState(localStorage.getItem('access_token'))
  const [loading, setLoading] = useState(true)

  const clearSession = useCallback(() => {
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    setToken(null)
    setUser(null)
  }, [])

  useEffect(() => {
    let active = true
    const handleTokenUpdated = (event) => {
      if (active) setToken(event.detail?.accessToken || null)
    }
    const handleSessionExpired = () => {
      if (active) clearSession()
    }
    window.addEventListener('auth-token-updated', handleTokenUpdated)
    window.addEventListener('auth-session-expired', handleSessionExpired)

    const init = async () => {
      const storedToken = localStorage.getItem('access_token')
      if (storedToken) {
        try {
          const res = await authAPI.me()
          if (active) setUser(normalizeUser(res.data))
        } catch (err) {
          console.error('Token验证失败:', err)
          if (active) clearSession()
        }
      }
      if (active) setLoading(false)
    }
    init()
    return () => {
      active = false
      window.removeEventListener('auth-token-updated', handleTokenUpdated)
      window.removeEventListener('auth-session-expired', handleSessionExpired)
    }
  }, [clearSession])

  const refreshSession = useCallback(async () => {
    try {
      return await refreshAccessToken()
    } catch (err) {
      console.error('Token刷新失败:', err)
      clearSession()
      return null
    }
  }, [clearSession])

  const login = async (username, password) => {
    const res = await authAPI.login(username, password)
    const { access_token, refresh_token, user: userData } = res.data

    localStorage.setItem('access_token', access_token)
    localStorage.setItem('refresh_token', refresh_token)

    setToken(access_token)
    setUser(normalizeUser(userData))
    return userData
  }

  const logout = async () => {
    try {
      await authAPI.logout()
    } catch (e) {
      /* ignore */
    }
    clearStoredSession()
    clearSession()
  }

  const updateUser = (userData) => {
    setUser((prev) => ({ ...prev, ...userData }))
  }

  return (
    <AuthContext.Provider value={{ user, token, loading, login, logout, refreshSession, updateUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
