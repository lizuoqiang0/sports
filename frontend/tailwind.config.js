/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // 商务蓝白主题
        brand: {
          50: '#eff6ff',
          100: '#dbeafe',
          200: '#bfdbfe',
          300: '#93c5fd',
          400: '#60a5fa',
          500: '#3b82f6',
          600: '#2563eb',
          700: '#1d4ed8',
          800: '#1e40af',
          900: '#1e3a8a',
        },
        ink: {
          50: '#f8fafc',
          100: '#f1f5f9',
          200: '#e2e8f0',
          300: '#cbd5e1',
          400: '#94a3b8',
          500: '#64748b',
          600: '#475569',
          700: '#334155',
          800: '#1e293b',
          900: '#0f172a',
          950: '#020617',
        },
        win: '#2563eb',
        lose: '#dc2626',
        draw: '#d97706',
        live: '#dc2626',
      },
      fontFamily: {
        sans: ['"DM Sans"', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'sans-serif'],
        display: ['Syne', 'PingFang SC', 'Hiragino Sans GB', 'sans-serif'],
      },
      boxShadow: {
        soft: '0 1px 0 rgba(15, 23, 42, 0.04)',
        lift: '0 8px 24px rgba(15, 23, 42, 0.06)',
      },
      animation: {
        'pulse-fast': 'pulse 1s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'odds-flash': 'flash 0.5s ease-in-out',
        'page-in': 'pageIn 0.45s cubic-bezier(0.22, 1, 0.36, 1) both',
      },
      keyframes: {
        flash: {
          '0%, 100%': { backgroundColor: 'transparent' },
          '50%': { backgroundColor: 'rgba(37, 99, 235, 0.12)' },
        },
        pageIn: {
          from: { opacity: '0', transform: 'translateY(10px)' },
          to: { opacity: '1', transform: 'translateY(0)' },
        },
      },
    },
  },
  plugins: [],
}
