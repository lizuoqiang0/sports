"""pytest 配置：mock 外部依赖（DB/Redis/DeepSeek），隔离测试环境。"""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from decimal import Decimal


@pytest.fixture
def mock_strategy_config():
    """构造默认策略配置。"""
    from app.ai.strategy import StrategyConfig
    return StrategyConfig(
        name="simple",
        max_bet_amount=100.0,
        max_daily_bets=10,
        stop_loss=500.0,
        take_profit=1000.0,
        use_llm_analysis=True,
        min_confidence=0.47,
        min_odds=1.65,
        max_odds=5.0,
    )


@pytest.fixture
def mock_match_info():
    """构造足球比赛信息。"""
    return {
        "id": 1001,
        "sport": "football",
        "league": "英超",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "home_score": 0,
        "away_score": 0,
        "clock": "27'",
        "period": "1H",
        "total_line": 2.5,
    }


@pytest.fixture
def mock_basketball_match_info():
    """构造篮球比赛信息（Q4 倒计时 8:30）。"""
    return {
        "id": 2001,
        "sport": "basketball",
        "league": "NBA",
        "home_team": "Lakers",
        "away_team": "Celtics",
        "home_score": 88,
        "away_score": 82,
        "clock": "8:30",
        "period": "Q4",
        "total_line": 180.5,
    }


@pytest.fixture
def mock_analysis_under():
    """构造 DeepSeek 分析结果：under 方向、已达成共识。"""
    return {
        "prediction": "under",
        "bet_type": "total",
        "confidence": 0.65,
        "odds": 1.85,
        "line": 2.5,
        "consensus_reached": True,
        "reasoning": "双方近期防守稳固，小球概率较高",
        "models_used": ["deepseek"],
    }


@pytest.fixture
def mock_analysis_over():
    """构造 DeepSeek 分析结果：over 方向（应被 A1 拒绝）。"""
    return {
        "prediction": "over",
        "bet_type": "total",
        "confidence": 0.70,
        "odds": 1.80,
        "line": 2.5,
        "consensus_reached": True,
        "reasoning": "双方进攻强势",
        "models_used": ["deepseek"],
    }


@pytest.fixture
def mock_analysis_no_consensus():
    """构造 DeepSeek 分析结果：未达成共识（应被 A2 拒绝）。"""
    return {
        "prediction": "under",
        "bet_type": "total",
        "confidence": 0.60,
        "odds": 1.85,
        "line": 2.5,
        "consensus_reached": False,
        "reasoning": "模型分歧较大",
        "models_used": ["deepseek"],
    }


@pytest.fixture
def mock_ai_config():
    """构造 AIConfig mock。"""
    cfg = MagicMock()
    cfg.strategy = "simple"
    cfg.max_bet_amount = 100.0
    cfg.max_daily_bets = 10
    cfg.min_confidence = 0.47
    cfg.stop_loss = 500.0
    cfg.take_profit = 1000.0
    cfg.min_odds = 1.65
    cfg.max_odds = 5.0
    cfg.use_llm_analysis = True
    cfg.is_active = True
    cfg.preferred_sports = ["football", "basketball"]
    cfg.excluded_teams = ["中国队", "北京国安"]
    return cfg


@pytest.fixture
def mock_cache():
    """Mock Redis cache。"""
    cache = AsyncMock()
    cache.get = AsyncMock(return_value=None)
    cache.set = AsyncMock(return_value=True)
    cache.set_json = AsyncMock(return_value=True)
    cache.get_json = AsyncMock(return_value=None)
    cache.delete = AsyncMock(return_value=True)
    cache.acquire_lock = AsyncMock(return_value=True)
    cache.extend_lock_if_owned = AsyncMock(return_value=True)
    cache.exists = AsyncMock(return_value=False)
    return cache


# ─── 盘口解析 fixtures ───


@pytest.fixture
def ob_hps_with_half_totals():
    """OB API hps 含全场 + 上下半场大小球。"""
    return [
        {
            "hpid": "2", "hpn": "全场大小",
            "hl": [{
                "hv": "2.5", "hid": "h001", "hs": "0",
                "ol": [
                    {"ot": "over", "on": "大2.5", "ov": "1.85", "oid": "o1", "os": 1},
                    {"ot": "under", "on": "小2.5", "ov": "1.75", "oid": "o2", "os": 1},
                ],
            }],
        },
        {
            "hpid": "7", "hpn": "上半场大小",
            "hl": [{
                "hv": "1.5", "hid": "h101", "hs": "0",
                "ol": [
                    {"ot": "over", "on": "大1.5", "ov": "1.90", "oid": "o3", "os": 1},
                    {"ot": "under", "on": "小1.5", "ov": "1.80", "oid": "o4", "os": 1},
                ],
            }],
        },
        {
            "hpid": "8", "hpn": "下半场大小",
            "hl": [{
                "hv": "2.0", "hid": "h201", "hs": "0",
                "ol": [
                    {"ot": "over", "on": "大2.0", "ov": "1.95", "oid": "o5", "os": 1},
                    {"ot": "under", "on": "小2.0", "ov": "1.85", "oid": "o6", "os": 1},
                ],
            }],
        },
    ]


@pytest.fixture
def ob_hps_half_under_only():
    """OB API hps 半场仅 under（无 over）。"""
    return [
        {
            "hpid": "7", "hpn": "上半场大小",
            "hl": [{
                "hv": "1.5", "hid": "h301", "hs": "0",
                "ol": [
                    {"ot": "under", "on": "小1.5", "ov": "1.80", "oid": "o7", "os": 1},
                ],
            }],
        },
    ]


@pytest.fixture
def pinnacle_period1_data():
    """平博 period 1 (上半场) 数据: [spread, total, moneyline]。"""
    return [
        [],  # spread
        [[0, 1.5, 1.90, 1.80, "s1"]],  # total: line=1.5, over=1.90, under=1.80
        [],  # moneyline
    ]


@pytest.fixture
def pinnacle_period2_under_only():
    """平博 period 2 (下半场) 仅 under 数据。"""
    return [
        [],
        [[0, 2.0, None, 1.85, "s2"]],  # over=None, under=1.85
        [],
    ]


@pytest.fixture
def mock_analysis_first_half_under():
    """DeepSeek 分析结果：上半场小球。"""
    return {
        "prediction": "under",
        "bet_type": "first_half_total",
        "confidence": 0.62,
        "odds": 1.80,
        "line": 2.5,
        "consensus_reached": True,
        "reasoning": "上半场双方保守，小球概率高",
        "models_used": ["deepseek"],
    }


@pytest.fixture
def mock_analysis_second_half_under():
    """DeepSeek 分析结果：下半场小球。"""
    return {
        "prediction": "under",
        "bet_type": "second_half_total",
        "confidence": 0.60,
        "odds": 1.85,
        "line": 2.5,
        "consensus_reached": True,
        "reasoning": "下半场节奏放缓",
        "models_used": ["deepseek"],
    }
