const SEL_LABEL = {
  under: '小球',
  home: '主队',
  away: '客队',
  draw: '平局',
}

function cleanReasonText(text) {
  return String(text || '')
    .replace(/^\[不投注\]\s*/u, '')
    .replace(/\s+/g, ' ')
    .trim()
}

export function formatAiRecommendationReason({ recommendation, analysis, strategy }) {
  const rec = recommendation || {}
  const ana = analysis || {}
  const strat = strategy || {}
  const raw = cleanReasonText(rec.reasoning || ana.reasoning || '')
  const selection = String(rec.selection || ana.prediction || '').toLowerCase()
  const selectionLabel = SEL_LABEL[selection] || '该方向'
  const conf = Number(rec.confidence ?? ana.confidence)
  const confPct = Number.isFinite(conf) ? `${(conf * 100).toFixed(0)}%` : null
  const minConf = Number(strat.min_confidence)
  const minConfPct = Number.isFinite(minConf) ? `${(minConf * 100).toFixed(0)}%` : null
  const odds = Number(rec.odds)
  const oddsText = Number.isFinite(odds) && odds > 0 ? odds.toFixed(2) : null
  const minOdds = Number(strat.min_odds)
  const maxOdds = Number(strat.max_odds)
  const rangeText = Number.isFinite(minOdds) && Number.isFinite(maxOdds)
    ? `${minOdds} - ${maxOdds}`
    : null

  if (rec.should_bet) {
    return raw || `AI 已确认 ${selectionLabel} 方向通过当前门槛，可以放行。`
  }
  if (raw.includes('置信度不足')) {
    return `AI 方向已识别为${selectionLabel}，但当前置信度 ${confPct || '--'} 低于放行门槛 ${minConfPct || '--'}，因此不放行。`
  }
  if (raw.includes('赔率不在') || raw.includes('赔率不在配置区间')) {
    return `AI 方向已识别为${selectionLabel}，但该方向当前赔率 ${oddsText || '--'} 不在允许区间 ${rangeText || '--'}，因此不放行。`
  }
  if (raw.includes('无可用盘口')) {
    return `AI 方向已识别为${selectionLabel}，但当前该方向没有可用盘口，因此不放行。`
  }
  if (raw.includes('共识不足')) {
    return `AI 已尝试分析这场比赛，但模型共识不足，因此这场不会放行。`
  }
  if (raw.includes('LLM 暂不可用') || raw.includes('LLM 分析超时')) {
    return `AI 方向暂时无法稳定确认，这场只保留参考，不放行下单。`
  }
  if (raw) {
    return `AI 方向已识别为${selectionLabel}，但该方向未同时满足置信度、赔率或风控门槛，因此不放行。当前原因：${raw}`
  }
  return `AI 方向已识别为${selectionLabel}，但该方向未同时满足置信度、赔率或风控门槛，因此不放行。`
}
