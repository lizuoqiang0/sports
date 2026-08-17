/** 赛事列表：虚拟盘过滤、去重、按时长排序 */

/** 解析比赛时钟为已进行秒数；用于列表按时长排序 */
export function matchElapsedSeconds(m) {
  const clock = String(m?.clock || '').trim()
  const period = String(m?.period || '')
  const matched = clock.match(/^(\d{1,3}):(\d{2})$/)
  if (!matched) return Number.MAX_SAFE_INTEGER
  let secs = Number(matched[1]) * 60 + Number(matched[2])
  if (period.includes('中场') && secs < 40 * 60) secs = 45 * 60
  else if (period.includes('加时上')) secs = 90 * 60 + secs
  else if (period.includes('加时下')) secs = 105 * 60 + secs
  else if (period.includes('加时')) secs = 90 * 60 + secs
  return secs
}

/** 前端兜底：过滤足球/篮球虚拟盘（VS-/EAFC/PANDA/NBA2K/手柄短时杯） */
export function isVirtualMatch(m) {
  const text = `${m?.league || ''} ${m?.home_team || ''} ${m?.away_team || ''}`
  return /eafc|ea\s*fc|fifa\s*\d{2}|panda\s*独家|nba\s*2k|2k\s*2[3-9]|vs\s*[-–-]|电子足球|电子篮球|虚拟|瓦尔基里|瓦尔哈拉|valkyrie|valhalla|手柄|gamepad/i.test(text)
    || /^\s*vs\b/i.test(String(m?.league || ''))
    || /\(\s*\d{1,2}\s*分钟\s*\)/.test(String(m?.league || ''))
}

/** 中国（含港澳台）国内足篮球赛事：不展示 */
export function isChinaMatch(m) {
  const league = String(m?.league || '')
  const teams = `${m?.home_team || ''} ${m?.away_team || ''}`
  const text = `${league} ${teams}`
  if (/中超|中甲|中乙|中冠|女超|女甲|足协杯|中国超级|中国甲级|中国乙级|中国足球|中国篮球|\bcsl\b|\bcba\b|\bwcba\b|港超|香港超级|台湾|台灣|澳门|澳門|中华台北|chinese\s*super\s*league|china\s*league|cfa\s*cup/i.test(text)) {
    return true
  }
  if (/中国|china/i.test(league)) return true
  if (/上海申花|上海海港|山东泰山|北京国安|梅州客家|青岛西海岸|青岛海牛|辽宁铁人|苏州东吴|成都蓉城|武汉三镇|天津津门虎|南通支云|深圳新鹏城|大连英博|广厦|首钢|同曦|上海大鲨鱼|辽宁飞豹/.test(teams)) {
    return true
  }
  return false
}

export function filterRealMatches(list) {
  return (list || []).filter((m) => {
    if (isVirtualMatch(m)) return false
    if (isChinaMatch(m)) return false
    const league = String(m?.league || '').trim()
    if (!league || league === '未知联赛' || league === '未知' || league === 'N/A' || league === '-' || league === '—') {
      return false
    }
    const sport = String(m?.sport || '').toLowerCase()
    if (sport !== 'football' && sport !== 'basketball' && sport !== 'soccer') return false
    // 进行中列表：必须已开赛（含「进行中/滚球」弱节次 + 比分/时钟）
    if (String(m?.status || '') === 'live') {
      const period = String(m?.period || '')
      const clock = String(m?.clock || '').trim()
      const hs = Number(m?.home_score || 0)
      const as = Number(m?.away_score || 0)
      const inplay = /上半场|下半场|中场|加时|进行中|滚球|第[1-4一二三四]节|Q[1-4]|1H|2H|HT|LIVE/i.test(period)
      const hasScore = (hs > 0 || as > 0) && !(hs <= 23 && [15, 30, 45].includes(as))
      const kickoffScore = hs <= 23 && [0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55].includes(as) && (as >= 15 || (hs >= 1 && (as === 0 || as === 30)))
      if (kickoffScore && !inplay) return false
      if (m?.start_time) {
        const t = Date.parse(m.start_time)
        if (Number.isFinite(t) && t > Date.now() + 2 * 60 * 1000) return false
      }
      if (!inplay && !clock && !hasScore && !m?.start_time) return false
    }
    const hs = Number(m?.home_score || 0)
    const as = Number(m?.away_score || 0)
    const period = String(m?.period || '')
    const looksBb = Math.max(hs, as) >= 20 || hs + as >= 40 || /第[1-4一二三四]节|Q[1-4]/i.test(period)
    if ((sport === 'football' || sport === 'soccer') && looksBb) return false
    return true
  })
}

export function sortMatchesByDuration(list) {
  const arr = [...filterRealMatches(list)]
  arr.sort((a, b) => matchElapsedSeconds(a) - matchElapsedSeconds(b))
  return arr
}
