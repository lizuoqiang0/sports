import { useEffect, useRef } from 'react'

/**
 * 页面可见时轮询；隐藏标签页暂停；卸载取消。
 * @param {() => void | Promise<void>} fn
 * @param {number} intervalMs
 * @param {{ enabled?: boolean, runOnMount?: boolean }} [opts]
 */
export function usePagePoll(fn, intervalMs, opts = {}) {
  const { enabled = true, runOnMount = false } = opts
  const fnRef = useRef(fn)
  fnRef.current = fn

  useEffect(() => {
    if (!enabled || !intervalMs || intervalMs < 0) return undefined

    let timer = null
    let cancelled = false

    const tick = () => {
      if (cancelled) return
      if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
      Promise.resolve(fnRef.current()).catch(() => {})
    }

    const start = () => {
      if (timer) clearInterval(timer)
      timer = setInterval(tick, intervalMs)
    }

    const onVis = () => {
      if (document.visibilityState === 'visible') {
        tick()
        start()
      } else if (timer) {
        clearInterval(timer)
        timer = null
      }
    }

    if (runOnMount) tick()
    if (typeof document === 'undefined' || document.visibilityState === 'visible') {
      start()
    }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      cancelled = true
      document.removeEventListener('visibilitychange', onVis)
      if (timer) clearInterval(timer)
    }
  }, [enabled, intervalMs, runOnMount])
}
