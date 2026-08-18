"""中国赛事过滤：不分析、不展示、不下注。"""
from __future__ import annotations

import re

# 联赛 / 杯赛 / 国家地区标记（命中任一即视为中国相关赛事）
_CHINA_LEAGUE_RE = re.compile(
    r"("
    r"中超|中甲|中乙|中冠|女超|女甲|足协杯|超足|"
    r"中青联|中青赛|中乙联赛|中甲联赛|"
    r"中国超级|中国甲级|中国乙级|中国足球|中国篮球|"
    r"chinese\s*super\s*league|\bcsl\b|china\s*league\s*one|china\s*league\s*two|"
    r"cfa\s*cup|chinese\s*fa\s*cup|"
    r"\bcba\b|\bwcba\b|中国男子篮球|中国女子篮球|全国男子篮球|"
    r"chinese\s*basketball|china\s*basketball|"
    r"港超|香港超级|香港甲组|香港联赛|"
    r"澳门|澳門|"
    r"台湾|台灣|"
    r"\bsbl\b|p\.?\s*league\s*\+?|t1\s*league|"
    r"中国\s*[A-Za-z]*\s*(联赛|杯|锦标赛|超级)"
    r")",
    re.IGNORECASE,
)

# 队名明显为中国大陆职业足球/篮球俱乐部时兜底（联赛字段缺失或被改写）
_CHINA_TEAM_RE = re.compile(
    r"("
    r"上海申花|上海海港|上海上港|山东泰山|北京国安|广州队|广州城|"
    r"天津津门虎|河南|成都蓉城|武汉三镇|浙江|长春亚泰|梅州客家|"
    r"青岛海牛|青岛西海岸|沧州雄狮|南通支云|深圳新鹏城|深圳|"
    r"大连英博|辽宁铁人|苏州东吴|重庆铜梁龙|云南玉昆|广西平果|"
    r"广东广州|新疆|辽宁飞豹|广厦|浙江稠州|上海久事|北京北控|"
    r"首钢|同曦|吉林东北虎|山西汾酒|青岛国信|广州龙狮|深圳马可波罗|"
    r"上海大鲨鱼|山东高速|福建浔兴|天津先行者"
    r")",
    re.IGNORECASE,
)


def is_china_match(
    league: str = "",
    home: str = "",
    away: str = "",
    sport: str = "",
) -> bool:
    """是否为中国（含港澳台）足球/篮球国内赛事，应全局排除。"""
    _ = sport
    league_s = str(league or "").strip()
    home_s = str(home or "").strip()
    away_s = str(away or "").strip()
    blob = f"{league_s} {home_s} {away_s}"
    if not blob.strip():
        return False
    if _CHINA_LEAGUE_RE.search(league_s) or _CHINA_LEAGUE_RE.search(blob):
        return True
    # 联赛名含「中国/China」且为足篮球语境
    if re.search(r"中国|china", league_s, flags=re.I):
        return True
    if _CHINA_TEAM_RE.search(home_s) or _CHINA_TEAM_RE.search(away_s):
        return True
    return False
