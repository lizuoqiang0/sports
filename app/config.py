"""
OB Sports Betting - 核心配置
"""
import json
from datetime import datetime, timezone, timedelta
from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import Any, Optional

# 本地时区（UTC+8 上海），用于"今日"投注统计的起始时间计算
LOCAL_TZ = timezone(timedelta(hours=8))


def today_start_utc() -> datetime:
    """返回本地时区当天 00:00 对应的 UTC 时间。

    DB 中 created_at 以 UTC 存储（DateTimeUTC 去掉 tzinfo 存为 naive UTC），
    因此查询"今日"注单时应以本地午夜对应的 UTC 时刻为起点。
    """
    now_local = datetime.now(LOCAL_TZ)
    local_midnight = datetime.combine(
        now_local.date(), datetime.min.time(), tzinfo=LOCAL_TZ
    )
    return local_midnight.astimezone(timezone.utc)


def month_start_utc() -> datetime:
    """返回本地时区当月 1 号 00:00 对应的 UTC 时间。"""
    now_local = datetime.now(LOCAL_TZ)
    month_first = now_local.date().replace(day=1)
    local_month_start = datetime.combine(
        month_first, datetime.min.time(), tzinfo=LOCAL_TZ
    )
    return local_month_start.astimezone(timezone.utc)


def _parse_str_list(v: Any) -> Any:
    """兼容 JSON 列表、逗号分隔，以及 Docker Compose 剥引号后的 [a,b] 形式。"""
    if v is None or isinstance(v, list):
        return v
    if isinstance(v, tuple):
        return [str(x).strip() for x in v if str(x).strip()]
    if not isinstance(v, str):
        return v
    s = v.strip()
    if not s:
        return []
    if s.startswith("["):
        try:
            data = json.loads(s)
            if isinstance(data, list):
                return [str(x).strip() for x in data if str(x).strip()]
        except json.JSONDecodeError:
            pass
        inner = s[1:-1].strip() if s.endswith("]") else s.strip("[]")
        return [p.strip().strip("\"'") for p in inner.split(",") if p.strip()]
    return [p.strip().strip("\"'") for p in s.split(",") if p.strip()]


class Settings(BaseSettings):
    # === 应用基础配置 ===
    APP_NAME: str = "OB Sports Betting Platform"
    APP_VERSION: str = "2.0.0"

    # === Redis ===
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_TTL: int = 300  # 5分钟
    REDIS_MAX_CONNECTIONS: int = 100

    # === 数据库 ===
    DATABASE_URL: str = "postgresql+asyncpg://ob_user:ob_password@localhost:5432/ob_sports"
    # workers×(pool+overflow) 勿超过 Postgres max_connections
    DB_POOL_SIZE: int = 8
    DB_MAX_OVERFLOW: int = 12
    DB_POOL_TIMEOUT: int = 20
    DB_POOL_RECYCLE: int = 1800

    # === 运行时并发（容器入口也可覆盖 UVICORN_WORKERS）===
    UVICORN_WORKERS: int = 4
    UVICORN_LIMIT_CONCURRENCY: int = 400
    UVICORN_BACKLOG: int = 2048
    UVICORN_KEEPALIVE: int = 5
    RUN_BACKGROUND_JOBS: bool = True  # 多 worker 时由 leader 选举决定实际执行者

    # === 安全 ===
    SECRET_KEY: str = "your-super-secret-key-change-in-production-min-32-chars"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALGORITHM: str = "HS256"
    BCRYPT_ROUNDS: int = 12

    # === CORS ===
    # 用 Any：避免 pydantic-settings 在 validator 前对 list 强制 json.loads。
    CORS_ORIGINS: Any = []

    # --- GPT API（唯一模型） ---
    GPT_API_KEY: Optional[str] = None
    GPT_BASE_URL: str = "https://xfastapi.ai/v1"
    GPT_MODEL: str = "gpt-5.6-terra"

    # --- AI 分析配置 ---
    GPT_TIMEOUT_SEC: float = 45.0          # 单次 GPT 分析总超时（外层 wait_for）
    LLM_CLIENT_TIMEOUT_SEC: float = 50.0   # OpenAI 客户端超时（须 ≥ GPT_TIMEOUT_SEC）
    LLM_MAX_TOKENS: int = 3072              # GPT 最大输出 tokens（确保多维度JSON不被截断）
    LLM_DEFAULT_CONFIDENCE: float = 0.33    # GPT 未返回置信度时的默认值
    LLM_TEMPERATURE: float = 0.35           # GPT 温度（0.2太低导致模板化，0.35平衡稳定与差异化）
    LLM_CACHE_TTL: int = 600               # 10分钟
    AI_SKIP_CACHE_TTL: int = 180           # GPT 判 skip 的负缓存（略超 120s 轮询间隔，跨轮生效）
    LLM_NEG_CACHE_TTL: int = 150           # 无共识结果负缓存（略超轮询间隔）
    H2H_MAX_MATCHES: int = 3               # 历史交锋最多保留场次
    FORM_MAX_MATCHES: int = 3               # 近况最多保留场次
    # 下单门槛（从 settings 读取，不写死）
    AI_MIN_CONFIDENCE: float = 0.47        # 下单最低置信度（含纯盘口模式）
    AI_MIN_ODDS: float = 1.65              # 下单最低赔率
    AI_MAX_ODDS: float = 5.00              # 下单最高赔率
    LIVE_ODDS_MAX_AGE_SEC: int = 90        # 滚球下单允许的赔率最大年龄

    AI_SCAN_INTERVAL_SEC: int = 120        # AI 引擎扫描间隔（秒，有候选时）
    AI_RECS_LIMIT: int = 80                # 推荐页分析上限
    AI_LIVE_SCAN_LIMIT: int = 120          # 自动引擎扫描上限
    AI_ANALYZE_CONCURRENCY: int = 8        # GPT 分析并发数
    AI_IDLE_RESCAN_SEC: int = 30           # 空轮快扫间隔（无候选时，捕捉刚开赛）
    AI_SKIP_COOLDOWN_SEC: int = 300        # 同场 LLM skip 冷却（避免 TTL 内重复调用）
    AI_ENABLE_OVER: bool = True            # 大球 over 下单开关（已启用：over 与 under 对等参与闸门评估）

    # 赛前上下文（交锋 / 近10场 / 伤病）
    AI_MATCH_CONTEXT_ENABLED: bool = True
    NOWSCORE_PROXY_URL: str = ""
    AI_MATCH_CONTEXT_IN_BATCH: bool = True
    AI_MATCH_CONTEXT_TTL_SEC: int = 21600  # 6h

    # === 投注规则 ===
    MIN_BET_AMOUNT: float = 100.0
    MAX_BET_AMOUNT: float = 100000.0
    # 数据保留时间（小时）：超过此时间的投注记录和赛事记录自动删除
    DATA_RETENTION_HOURS: int = 24
    MAX_DAILY_BETS: int = 50

    # === AI自动投注配置 ===
    AI_ENABLED: bool = True
    # 策略默认参数（用户 AIConfig 未设置时的回退值）
    AI_STRATEGY_MAX_BET_AMOUNT: float = 100.0   # 策略单笔最大金额
    AI_STRATEGY_MAX_DAILY_BETS: int = 10        # 策略每日注数
    AI_STOP_LOSS: float = 500.0           # 日止损绝对金额
    AI_TAKE_PROFIT: float = 1000.0        # 日止盈绝对金额
    # 下单失败补单（重试）
    BET_RETRY_COUNT: int = 2              # 下单失败重试次数
    BET_RETRY_DELAY: float = 3.0          # 重试间隔（秒）
    AI_RETRY_SLEEP_SEC: int = 60             # 引擎异常后重试休眠
    AI_MIN_BALANCE: float = 10.0             # 最低可用余额阈值
    # 捷报比分
    NOWSCORE_BASE_URL: str = "https://m.nowscore.com"
    NOWSCORE_TITLE_CACHE_TTL: int = 1800    # 标题缓存 TTL（秒）
    NOWSCORE_TITLE_BATCH_SIZE: int = 20     # 并发获取标题每批数量
    # nowscore 当日全量预取（批量缓存所有比赛上下文，AI 分析时直接读 Redis）
    NOWSCORE_PREFETCH_ENABLED: bool = True  # 预取开关（打开：AI 分析时读取 nowscore 基本面，提升置信度）
    NOWSCORE_PREFETCH_INTERVAL_SEC: int = 3600  # 预取间隔（秒，默认 1h）
    NOWSCORE_PREFETCH_CONCURRENCY: int = 10  # 预取并发数
    # 上下文缓存
    AI_CONTEXT_NONE_TTL: int = 900          # source=none 时短缓存 TTL
    # API 参数
    AI_DEFAULT_STAKE: float = 100.0           # 默认下注金额
    AI_RECS_MAX_LIMIT: int = 200             # 推荐列表最大上限
    # 风险评分权重
    AI_RISK_LOW_CONF_WEIGHT: float = 0.4     # 低置信度风险权重
    AI_RISK_HIGH_ODDS_THRESHOLD: float = 5.0 # 高赔率风险阈值
    AI_RISK_HIGH_ODDS_PENALTY: float = 0.3   # 高赔率风险增量
    AI_RISK_MID_ODDS_THRESHOLD: float = 3.0  # 中赔率风险阈值
    AI_RISK_MID_ODDS_PENALTY: float = 0.15   # 中赔率风险增量
    AI_RISK_ACTIVE_PENALTY: float = 0.02     # 每笔持仓风险系数
    AI_RISK_ACTIVE_CAP: float = 0.1          # 持仓风险上限

    # === 日志 ===
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "/app/logs/app.log"

    # === 赔率监控 / 机会扫描（规格对齐）===
    MONITORING_ENABLED: bool = True
    # 默认下单模式：manual=人工 / active=自动（可被用户开关覆盖）
    DEFAULT_BET_MODE: str = "manual"
    ALLOWED_HOSTS: Any = []
    ALLOW_PUBLIC_REGISTER: bool = False
    # 内部服务鉴权（Backend ↔ Browser Gate）
    INTERNAL_API_TOKEN: str = ""

    # === Browser Gate / 站点浏览器 ===
    BOOKMAKER_BROWSER_GATE_URL: str = ""

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", mode="before")
    @classmethod
    def _coerce_str_lists(cls, v: Any) -> Any:
        return _parse_str_list(v)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",  # 允许 compose/.env 中的运维变量不阻塞启动
    }


settings = Settings()
