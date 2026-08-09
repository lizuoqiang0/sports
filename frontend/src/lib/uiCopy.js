export function formatIntervalLabel(seconds) {
  const sec = Math.max(1, Number(seconds) || 30)
  if (sec % 60 === 0) {
    const minutes = sec / 60
    return `${minutes} 分钟`
  }
  return `${sec} 秒`
}
