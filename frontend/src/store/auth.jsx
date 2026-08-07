import { createContext, useContext, useState, useEffect } from 'react'
import { authAPI } from '../lib/api.js'

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

  useEffect(() => {
    const init = async () => {
      const storedToken = localStorage.getItem('access_token')
      if (storedToken) {
        try {
          const res = await authAPI.me()
          setUser(normalizeUser(res.data))
        } catch (err) {
          console.error('Token验证失败:', err)
          localStorage.removeItem('access_token')
          localStorage.removeItem('refresh_token')
        }
      }
      setLoading(false)
    }
    init()
  }, [])

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
    localStorage.removeItem('access_token')
    localStorage.removeItem('refresh_token')
    setToken(null)
    setUser(null)
  }

  const updateUser = (userData) => {
    setUser((prev) => ({ ...prev, ...userData }))
  }

  return (
    <AuthContext.Provider value={{ user, token, loading, login, logout, updateUser }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
