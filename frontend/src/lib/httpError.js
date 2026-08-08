export function extractErrorMessage(err, fallback = '请求失败，请稍后重试') {
  const candidates = [
    err?.detail,
    err?.message,
    err?.error_code,
    err?.response?.data?.detail,
    err?.response?.data?.message,
  ]
  for (const item of candidates) {
    if (typeof item === 'string' && item.trim()) return item.trim()
  }
  return fallback
}
