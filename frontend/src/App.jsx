import { Routes, Route, Navigate } from 'react-router-dom'
import { useAuth } from './store/auth.jsx'
import { WSProvider } from './store/websocket.jsx'
import Layout from './components/Layout.jsx'

// 页面
import LoginPage from './pages/Login.jsx'
import MatchesPage from './pages/Matches.jsx'
import MatchDetailPage from './pages/MatchDetail.jsx'
import PortfolioPage from './pages/Portfolio.jsx'
import AIPanelPage from './pages/AIPanel.jsx'
import DashboardPage from './pages/Dashboard.jsx'
import BookmakersPage from './pages/Bookmakers.jsx'
import LogsPage from './pages/Logs.jsx'

function ProtectedRoute({ children }) {
  const { user, loading } = useAuth()
  if (loading) return (
    <div className="flex flex-col items-center justify-center h-screen bg-[#f6f8fb] gap-3">
      <div className="animate-spin w-6 h-6 border-2 border-ink-200 border-t-ink-800 rounded-full" />
      <p className="text-sm font-medium text-ink-500 tracking-wide">
        正在加载工作台…
      </p>
    </div>
  )
  if (!user) return <Navigate to="/login" replace />
  return children
}

export default function App() {
  const { user } = useAuth()

  return (
    <WSProvider>
      <Routes>
        <Route path="/login" element={user ? <Navigate to="/" replace /> : <LoginPage />} />
        <Route path="/" element={<ProtectedRoute><Layout /></ProtectedRoute>}>
          <Route index element={<DashboardPage />} />
          <Route path="matches" element={<MatchesPage />} />
          <Route path="matches/:id" element={<MatchDetailPage />} />
          <Route path="portfolio" element={<PortfolioPage />} />
          <Route path="ai" element={<AIPanelPage />} />
          <Route path="bookmakers" element={<BookmakersPage />} />
          <Route path="logs" element={<LogsPage />} />
        </Route>
      </Routes>
    </WSProvider>
  )
}
