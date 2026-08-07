/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      colors: {
        // Pitch Signal：赛场墨绿 + 冷灰墨色
        brand: {
          50: '#eef8f2',
          100: '#d5efdf',
          200: '#aedfbf',
          300: '#7bc79a',
          400: '#47a873',
          500: '#268a56',
          600: '#1f7a4c',
          700: '#1a6340',
          800: '#174f35',
          900: '#12412c',
        },
        ink: {
          50: '#f5f7f6',
          100: '#e6ebe9',
          200: '#cfd8d4',
          300: '#a8b7b1',
          400: '#7a8f87',
          500: '#5a6f67',
          600: '#455853',
          700: '#364540',
          800: '#1c2a25',
          900: '#101820',
          950: '#0a1014',
        },
        win: '#1f7a4c',
        lose: '#c0392b',
        draw: '#b7791f',
        live: '#c0392b',
      },
      fontFamily: {
        sans: ['"DM Sans"', 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', 'sans-serif'],
        display: ['Syne', 'PingFang SC', 'Hiragino Sans GB', 'sans-serif'],
      },
      boxShadow: {
        soft: '0 1px 0 rgba(16, 24, 32, 0.04)',
        lift: '0 8px 24px rgba(16, 24, 32, 0.06)',
      },
      animation: {
        'pulse-fast': 'pulse 1s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'odds-flash': 'flash 0.5s ease-in-out',
        'page-in': 'pageIn 0.45s cubic-bezier(0.22, 1, 0.36, 1) both',
      },
      keyframes: {
        flash: {
          '0%, 100%': { backgroundColor: 'transparent' },
          '50%': { backgroundColor: 'rgba(31, 122, 76, 0.12)' },
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
