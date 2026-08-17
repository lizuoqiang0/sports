import axios from 'axios'

const API_BASE = '/api/v1'

// 创建axios实例
export const api = axios.create({
  baseURL: API_BASE,
  timeout: 15000,
})

// 请求拦截器 - 自动注入Token
api.interceptors.request.use((config) => {
  const token = localStorage.getItem('access_token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截器 - 统一错误处理 + Token刷新
api.interceptors.response.use(
  (res) => res.data,  // 直接返回data层
  async (err) => {
    const original = err.config || {}
    if (err.response?.status === 401 && !original._retry) {
      original._retry = true
      const refreshToken = localStorage.getItem('refresh_token')
      if (refreshToken) {
        try {
          // 用裸 axios，避免走 unwrap 拦截器
          const res = await axios.post(`${API_BASE}/auth/refresh`, { refresh_token: refreshToken })
          const { access_token, refresh_token } = res.data.data
          localStorage.setItem('access_token', access_token)
          localStorage.setItem('refresh_token', refresh_token)
          original.headers = original.headers || {}
          original.headers.Authorization = `Bearer ${access_token}`
          // 重试走同一 api 实例，保持 data 解包
          return api.request(original)
        } catch (refreshErr) {
          localStorage.removeItem('access_token')
          localStorage.removeItem('refresh_token')
          window.location.href = '/login'
        }
      } else {
        window.location.href = '/login'
      }
    }
    return Promise.reject(err.response?.data || err)
  }
)

// === API方法封装 ===
export const authAPI = {
  register: (data) => api.post('/auth/register', data),
  login: (username, password) => api.post('/auth/login', { username, password }),
  me: () => api.get('/auth/me'),
  logout: () => api.post('/auth/logout'),
}

export const matchesAPI = {
  list: (params) => api.get('/matches', { params }),
  detail: (id) => api.get(`/matches/${id}`),
  search: (q) => api.get('/matches/search', { params: { q } }),
  live: () => api.get('/matches/live/now'),
}

export const oddsAPI = {
  getMatch: (matchId) => api.get(`/matches/${matchId}/odds`),
}

export const betsAPI = {
  place: (data) => api.post('/bets/place', data),
  history: (params) => api.get('/bets/history', { params }),
  portfolio: () => api.get('/bets/portfolio', { timeout: 45000 }),
  resetPnl: () => api.post('/bets/portfolio/reset-pnl', null, { timeout: 30000 }),
}

export const aiAPI = {
  config: () => api.get('/ai/config'),
  updateConfig: (data) => api.put('/ai/config', data),
  start: () => api.post('/ai/start', null, { timeout: 30000 }),
  stop: () => api.post('/ai/stop'),
  status: () => api.get('/ai/status'),
  recommend: (matchId) =>
    api.get(`/ai/recommend/${matchId}`, { params: {}, timeout: 120000 }),
  recommendations: (sport, limit, refresh = false, provider = '') =>
    api.get('/ai/recommendations', {
      params: { sport, limit, refresh, provider: provider || undefined },
      // 后台预分析：接口秒回缓存/进度，不再阻塞等 LLM
      timeout: 30000,
    }),
  startAnalysis: (sport, limit = 80) =>
    api.post('/ai/recommendations/start', null, {
      params: { sport, limit },
      timeout: 30000,
    }),
  stopAnalysis: (sport) =>
    api.post('/ai/recommendations/stop', null, {
      params: { sport },
      timeout: 15000,
    }),
  prepareRecommendations: (sport, limit = 80, provider = '') =>
    api.get('/ai/recommendations', {
      params: { sport, limit, refresh: false, provider: provider || undefined },
      timeout: 20000,
    }),
  oneClickBet: (matchId, stake = 100, markets = []) =>
    api.post(`/ai/one-click-bet/${matchId}`, { stake, markets }, { timeout: 30000 }),
}

export const bookmakersAPI = {
  list: () => api.get('/bookmakers'),
  balances: () => api.get('/bookmakers/balances'),
  catalog: () => api.get('/bookmakers/catalog'),
  save: (accounts) => api.put('/bookmakers', { accounts }),
  verify: (code) =>
    api.post(`/bookmakers/${code}/verify`, null, {
      timeout: 130000,
      params: { manual_venue: false },
    }),
  verifyBatch: (codes) =>
    api.post(
      '/bookmakers/verify-batch',
      { codes: codes || null, manual_venue: false },
      { timeout: 180000 },
    ),
  disconnect: (code) => api.post(`/bookmakers/${code}/disconnect`, null, { timeout: 15000 }),
  syncLive: () => api.post('/bookmakers/sync-live', null, { timeout: 210000 }),
  gateHealth: () => api.get('/bookmakers/gate-health', { timeout: 5000 }),
  oddsCompare: (matchId) => api.get(`/bookmakers/odds-compare/${matchId}`),
}

export const monitoringAPI = {
  getBetMode: () => api.get('/monitoring/bet-mode'),
  setBetMode: (betMode) => api.put('/monitoring/bet-mode', { bet_mode: betMode }),
}

export const adminAPI = {
  getDataSourceSwitch: () => api.get('/admin/nowscore/switch'),
  setDataSourceSwitch: (enabled) => api.post(`/admin/nowscore/switch?enabled=${enabled}`),
  triggerPrefetch: (sport = 'all') => api.post(`/admin/nowscore/prefetch?sport=${sport}`),
  getPrefetchProgress: () => api.get('/admin/nowscore/progress'),
  getAliasCandidates: (sport = 'all', limit = 100, minScore = 0) =>
    api.get('/admin/nowscore/alias-candidates', { params: { sport, limit, min_score: minScore } }),
  approveAliasCandidate: (candidateId, deleteCandidate = true) =>
    api.post('/admin/nowscore/alias-candidates/approve', null, {
      params: { candidate_id: candidateId, delete_candidate: deleteCandidate },
    }),
  approveAliasCandidatesBatch: (candidateIds = [], deleteCandidate = true) =>
    api.post('/admin/nowscore/alias-candidates/approve-batch', {
      candidate_ids: candidateIds,
      delete_candidate: deleteCandidate,
    }),
  getAliasOverrides: (sport = 'all', limit = 100) =>
    api.get('/admin/nowscore/alias-overrides', { params: { sport, limit } }),
  exportAliasOverrides: (sport = 'all', limit = 5000) =>
    api.get('/admin/nowscore/alias-overrides/export', { params: { sport, limit } }),
  previewAliasOverridesImport: (items = []) =>
    api.post('/admin/nowscore/alias-overrides/import-preview', { items }),
  importAliasOverrides: (items = []) =>
    api.post('/admin/nowscore/alias-overrides/import', { items }),
  getAliasAuditLogs: (limit = 100) =>
    api.get('/admin/nowscore/alias-audit-logs', { params: { limit } }),
  deleteAliasOverride: (recordId) =>
    api.delete('/admin/nowscore/alias-overrides', { params: { record_id: recordId } }),
  deleteAliasOverridesBatch: (recordIds = []) =>
    api.delete('/admin/nowscore/alias-overrides', { data: { record_ids: recordIds } }),
}
