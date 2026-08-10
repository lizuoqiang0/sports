"""
OB Sports Betting - 核心配置
"""
import json
from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import Any, Optional


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


def _parse_debug_flag(v: Any) -> Any:
    """兼容部署环境里常见的 DEBUG 写法，如 release/debug。"""
    if isinstance(v, bool) or v is None:
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if not isinstance(v, str):
        return v
    s = v.strip().lower()
    if s in {"1", "true", "yes", "on", "debug", "dev", "development"}:
        return True
    if s in {"0", "false", "no", "off", "release", "prod", "production", "staging"}:
        return False
    return v


class Settings(BaseSettings):
    # === 应用基础配置 ===
    APP_NAME: str = "OB Sports Betting Platform"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False
    ENVIRONMENT: str = "production"  # production / staging / development
    # 强制真实场景：禁止 .demo、模拟器、dry_run / simulate（本地调试可设 false）
    FORCE_LIVE_MODE: bool = True

    # === Redis ===
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_CACHE_TTL: int = 300  # 5分钟
    REDIS_MAX_CONNECTIONS: int = 100

    # === 数据库 ===
    DATABASE_URL: str = "postgresql+asyncpg://ob_user:ob_password@localhost:5432/ob_sports"
    # 线上建议 8/12：workers×(pool+overflow) 勿超过 Postgres max_connections
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
    # 用 Any：避免 pydantic-settings 在 validator 前对 list 强制 json.loads
    # （Docker Compose 会把 ["http://x"] 剥成 [http://x]）
    CORS_ORIGINS: Any = [
        "http://localhost:3000",
    ]

    # === AI/LLM 配置（六模型各自独立 Key + Base URL + Model）===
    # 可选兜底：某模型未单独配置 Key/URL 时回退到这两项
    NEWAPI_API_KEY: Optional[str] = None
    NEWAPI_BASE_URL: str = "https://www.juaiapi.com/v1"

    # --- GPT API（唯一模型） ---
    GPT_API_KEY: Optional[str] = None
    GPT_BASE_URL: str = "https://xfastapi.ai/v1"
    GPT_MODEL: str = "gpt-5.6-terra"

    # --- AI 分析配置 ---
    LLM_CLIENT_TIMEOUT_SEC: float = 90.0   # GPT API 调用超时（大 prompt 需要更长时间）
    LLM_MAX_TOKENS: int = 2048              # GPT 最大输出 tokens（确保 JSON 不被截断）
    LLM_DEFAULT_CONFIDENCE: float = 0.33    # GPT 未返回置信度时的默认值
    LLM_TEMPERATURE: float = 0.2            # GPT 温度
    LLM_NO_DATA_CONFIDENCE_CAP: float = 0.55  # 无数据时置信度上限
    LLM_UNAVAILABLE_CONF_CAP: float = 0.49   # LLM不可用时置信度上限
    LLM_CACHE_TTL: int = 600               # 10分钟
    # Prompt 压缩：截断冗余数据，控制 prompt 在 8KB 以内
    PROMPT_MAX_CHARS: int = 8000           # prompt 最大字符数
    H2H_MAX_MATCHES: int = 3               # 历史交锋最多保留场次
    FORM_MAX_MATCHES: int = 3               # 近况最多保留场次
    # 下单门槛（从 settings 读取，不写死）
    AI_MIN_CONFIDENCE: float = 0.47        # 下单最低置信度（含纯盘口模式）
    AI_MIN_ODDS: float = 1.65              # 下单最低赔率
    AI_MAX_ODDS: float = 5.00              # 下单最高赔率

    AI_SCAN_INTERVAL_SEC: int = 120        # AI 引擎扫描间隔（秒）
    AI_RECS_LIMIT: int = 80                # 推荐页分析上限
    AI_LIVE_SCAN_LIMIT: int = 120          # 自动引擎扫描上限
    AI_ANALYZE_CONCURRENCY: int = 8        # GPT 分析并发数

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
    MAX_BETS_PER_MATCH: int = 5
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
    OB_BET_VERIFY_INITIAL_DELAY_SEC: float = 4.0  # OB 下单后首次验证等待
    OB_BET_VERIFY_RETRIES: int = 8               # OB 下单后最多验证次数
    OB_BET_VERIFY_INTERVAL_SEC: float = 2.0      # OB 下单后每次验证间隔
    OB_BET_VERIFY_HISTORY_DAYS: int = 1          # OB 注单历史查询天数
    AI_RETRY_SLEEP_SEC: int = 60             # 引擎异常后重试休眠
    AI_MIN_BALANCE: float = 10.0             # 最低可用余额阈值
    # 捷报比分
    NOWSCORE_BASE_URL: str = "https://m.nowscore.com"
    NOWSCORE_TITLE_CACHE_TTL: int = 1800    # 标题缓存 TTL（秒）
    NOWSCORE_TITLE_BATCH_SIZE: int = 20     # 并发获取标题每批数量
    # nowscore 当日全量预取（批量缓存所有比赛上下文，AI 分析时直接读 Redis）
    NOWSCORE_PREFETCH_ENABLED: bool = False  # 预取开关（默认关闭）
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
    AI_DIVERSIFY_MAX_PER_LEAGUE: int = 3     # 同一联赛最多注数

    # === WebSocket ===
    WS_HEARTBEAT_INTERVAL: int = 30
    WS_MAX_CONNECTIONS_PER_USER: int = 5

    # === 日志 ===
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = "/app/logs/app.log"

    # === 赔率监控 / 机会扫描（规格对齐）===
    MONITORING_ENABLED: bool = True
    MAX_QUOTE_AGE_SEC: int = 30
    MIN_MATCH_CONFIDENCE: float = 0.90
    MIN_NET_PROFIT_RATE: float = 0.005
    MIN_WORST_CASE_PROFIT: float = 1.0
    MAX_STAKE_PER_EVENT: float = 5000.0
    MAX_EXPOSURE_PER_SOURCE: float = 10000.0
    MAX_EXPOSURE_PER_ACCOUNT: float = 20000.0
    MAX_DAILY_LOSS: float = 2000.0
    # 默认下单模式：manual=人工 / active=自动（可被用户开关覆盖）
    DEFAULT_BET_MODE: str = "manual"
    ALLOWED_HOSTS: Any = ["localhost", "127.0.0.1", "backend", "ob-backend"]
    WEAK_SECRET_BLOCK_IN_PROD: bool = True
    # 生产关闭开放注册；需要时在 .env 设 true
    ALLOW_PUBLIC_REGISTER: bool = False
    # 生产默认关闭 /docs /redoc；排障可临时 true
    EXPOSE_API_DOCS: bool = False
    # 内部服务鉴权（Backend ↔ Browser Gate）
    INTERNAL_API_TOKEN: str = ""

    # === Browser Gate / 站点浏览器 ===
    BOOKMAKER_BROWSER_GATE_URL: str = ""
    BOOKMAKER_BROWSER_HEADLESS: str = "0"
    BOOKMAKER_MANUAL_VENUE: str = "0"
    BOOKMAKER_DISABLE_SITES: str = ""           # 临时关闭站点（ob,pinnacle）
    BROWSER_GATE_PORT: int = 9277
    BOOKMAKER_BACKEND_URL: str = "http://127.0.0.1:8000"
    GATE_HOST: str = "0.0.0.0"

    @field_validator("CORS_ORIGINS", "ALLOWED_HOSTS", mode="before")
    @classmethod
    def _coerce_str_lists(cls, v: Any) -> Any:
        return _parse_str_list(v)

    @field_validator("DEBUG", mode="before")
    @classmethod
    def _coerce_debug_flag(cls, v: Any) -> Any:
        return _parse_debug_flag(v)

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",  # 允许 compose/.env 中的运维变量不阻塞启动
    }


settings = Settings()
