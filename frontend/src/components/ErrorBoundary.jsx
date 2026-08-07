import { Component } from 'react'

export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props)
    this.state = { hasError: false, message: '' }
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, message: error?.message || '未知错误' }
  }

  componentDidCatch(error, info) {
    console.error('UI crash:', error, info)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-white text-gray-900 p-6">
          <div className="max-w-md text-center space-y-4">
            <h1 className="text-xl font-bold">页面出了点问题</h1>
            <p className="text-sm text-gray-500">{this.state.message}</p>
            <button
              className="btn-primary px-5 py-2"
              onClick={() => {
                this.setState({ hasError: false, message: '' })
                window.location.href = '/'
              }}
            >
              返回首页
            </button>
          </div>
        </div>
      )
    }
    return this.props.children
  }
}
