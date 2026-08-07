/** 安全格式化金额/数值（兼容后端 Decimal 字符串） */
export function formatMoney(value, digits = 2) {
  const n = Number(value)
  if (!Number.isFinite(n)) return (0).toFixed(digits)
  return n.toFixed(digits)
}

export function formatPercent(value, digits = 0) {
  const n = Number(value)
  if (!Number.isFinite(n)) return (0).toFixed(digits)
  return n.toFixed(digits)
}

export function toNumber(value, fallback = 0) {
  const n = Number(value)
  return Number.isFinite(n) ? n : fallback
}

/** 球类中文标签：仅足球 / 篮球 */
export function sportLabel(sport) {
  const s = String(sport || '').toLowerCase()
  if (s === 'football' || s === 'soccer') return '足球'
  if (s === 'basketball') return '篮球'
  return '未知球类'
}
