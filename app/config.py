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

    DOUBAO_API_KEY: Optional[str] = None
    DOUBAO_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/plan/v3"
    DOUBAO_MODEL: str = "doubao-seed-2.0-lite"

    GPT_API_KEY: Optional[str] = None
    GPT_BASE_URL: str = "https://www.juaiapi.com/v1"
    GPT_MODEL: str = "gpt-5.4"

    DEEPSEEK_API_KEY: Optional[str] = None
    DEEPSEEK_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/plan/v3"
    DEEPSEEK_MODEL: str = "deepseek-v4-pro"

    KIMI_API_KEY: Optional[str] = None
    KIMI_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/plan/v3"
    KIMI_MODEL: str = "kimi-k2.6"

    MINIMAX_API_KEY: Optional[str] = None
    MINIMAX_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/plan/v3"
    MINIMAX_MODEL: str = "minimax-m3"

    GLM_API_KEY: Optional[str] = None
    GLM_BASE_URL: str = "https://ark.cn-beijing.volces.com/api/plan/v3"
    GLM_MODEL: str = "glm-5.2"

    # Ensemble 共识过滤
    ENSEMBLE_MIN_CONSENSUS: float = 0.67   # 同意占比门槛（2/3 多数）
    ENSEMBLE_MIN_VOTES: int = 2            # 可用模型中最少同意票
    ENSEMBLE_CONCURRENCY: int = 6          # 批量同场分析并发（推荐页加速）
    ENSEMBLE_TIMEOUT_SEC: float = 30.0     # 单场 ensemble 总超时
    LLM_CLIENT_TIMEOUT_SEC: float = 15.0   # 单模型 LLM API 调用超时
    LLM_RETRY_TIMEOUT_SEC: float = 8.0     # 重试时的缩短超时（临时性错误才重试）
    ENSEMBLE_QUORUM: int = 2               # 收到 N 个成功投票即可早退（不必等慢模型）
    ENSEMBLE_MAX_MODELS: int = 3           # 单场最多并行调用的模型数
    # 优先快模型，按速度排序
    ENSEMBLE_MODEL_ORDER: str = "deepseek,doubao,gpt,minimax,glm,kimi"
    AI_SCAN_INTERVAL_SEC: int = 600        # AI 引擎扫描间隔（秒），默认 10 分钟
    AI_MAX_BETS_PER_CYCLE: int = 3         # 自动模式每轮最多下单笔数（须为不同比赛）
    AI_RECS_LIMIT: int = 80                # 每批分析同场上限（尽量覆盖全部滚球）
    AI_LIVE_SCAN_LIMIT: int = 120          # 自动引擎每轮扫描同场上限

    # LLM缓存
    LLM_CACHE_TTL: int = 600  # 10分钟

    # 赛前上下文（交锋 / 近10场 / 伤病）- 仅真实数据源，不编造
    AI_MATCH_CONTEXT_ENABLED: bool = True
    # nowscore 代理（留空则 httpx 自动读取系统代理）
    NOWSCORE_PROXY_URL: str = ""
    # 批量推荐时也获取赛前上下文
    AI_MATCH_CONTEXT_IN_BATCH: bool = True
    AI_MATCH_CONTEXT_TTL_SEC: int = 21600  # 6h

    # === 投注规则 ===
    MIN_BET_AMOUNT: float = 100.0
    MAX_BET_AMOUNT: float = 100000.0
    # 数据保留时间（小时）：超过此时间的投注记录和赛事记录自动删除
    DATA_RETENTION_HOURS: int = 24

    @property
    def one_click_min_stake(self) -> float:
        """一键投注最低金额：与 MIN_BET_AMOUNT 一致。"""
        return float(self.MIN_BET_AMOUNT)
    MAX_BETS_PER_MATCH: int = 5
    MAX_DAILY_BETS: int = 50

    # === AI自动投注配置 ===
    AI_ENABLED: bool = True
    AI_MAX_BET_PERCENTAGE: float = 0.05  # 单次最多用5%余额
    AI_DAILY_LOSS_LIMIT: float = 0.20     # 日亏损上限20%
    AI_MIN_CONFIDENCE: float = 0.75       # 最低置信度
    AI_RISK_LEVEL: str = "moderate"        # conservative / moderate / aggressive
    # 策略默认参数（用户 AIConfig 未设置时的回退值）
    AI_MAX_ODDS: float = 10.0             # 策略赔率上限
    AI_MIN_ODDS: float = 1.1              # 策略赔率下限
    AI_STRATEGY_MAX_BET_AMOUNT: float = 100.0   # 策略单笔最大金额
    AI_STRATEGY_MAX_DAILY_BETS: int = 10        # 策略每日注数
    AI_STOP_LOSS: float = 500.0           # 日止损绝对金额
    AI_TAKE_PROFIT: float = 1000.0        # 日止盈绝对金额
    AI_KELLY_FRACTION_CAP: float = 0.25   # 凯利值上限
    AI_MAX_CONCURRENT_BETS: int = 10      # 最大同时持仓数
    # 下单失败补单（重试）
    BET_RETRY_COUNT: int = 2              # 下单失败重试次数
    BET_RETRY_DELAY: float = 3.0          # 重试间隔（秒）
    AI_TAKE_PROFIT_PCT: float = 0.50      # 止盈百分比
    AI_SINGLE_MODEL_MIN_CONFIDENCE: float = 0.70  # 单模型共识放行阈值
    # conservative 预设
    AI_CONS_MAX_BET_PCT: float = 0.02
    AI_CONS_DAILY_LOSS: float = 0.10
    AI_CONS_MAX_CONCURRENT: int = 5
    AI_CONS_MIN_CONFIDENCE: float = 0.80
    AI_CONS_KELLY_CAP: float = 0.10
    # aggressive 预设
    AI_AGG_MAX_BET_PCT: float = 0.10
    AI_AGG_DAILY_LOSS: float = 0.35
    AI_AGG_MAX_CONCURRENT: int = 20
    AI_AGG_MIN_CONFIDENCE: float = 0.70
    AI_AGG_KELLY_CAP: float = 0.30
    # LLM 调用参数
    LLM_TEMPERATURE: float = 0.2              # LLM 采样温度
    LLM_MAX_TOKENS: int = 2048               # LLM 最大输出 token
    LLM_DEFAULT_CONFIDENCE: float = 0.33     # 模型未返回置信度时的默认值
    LLM_NO_DATA_CONFIDENCE_CAP: float = 0.55 # 无真实数据时 LLM 置信度上限
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
    AI_DEFAULT_CASHOUT_THRESHOLD: float = 0.8  # 默认提前兑现阈值
    AI_DEFAULT_STAKE: float = 100.0           # 默认下注金额
    AI_RECS_MAX_LIMIT: int = 200             # 推荐列表最大上限
    # 风险评分权重
    AI_RISK_LOW_CONF_WEIGHT: float = 0.4     # 低置信度风险权重
    AI_RISK_HIGH_ODDS_THRESHOLD: float = 5.0 # 高赔率风险阈值
    AI_RISK_HIGH_ODDS_PENALTY: float = 0.3   # 高赔率风险增量
    AI_RISK_MID_ODDS_THRESHOLD: float = 3.0  # 中赔率风险阈值
    AI_RISK_MID_ODDS_PENALTY: float = 0.15   # 中赔率风险增量
    AI_RISK_LOW_EV_THRESHOLD: float = 0.05   # 低 EV 风险阈值
    AI_RISK_LOW_EV_PENALTY: float = 0.2      # 低 EV 风险增量
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

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",  # 允许 compose/.env 中的运维变量不阻塞启动
    }


settings = Settings()
