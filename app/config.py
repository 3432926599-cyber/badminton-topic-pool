"""全局配置：路径、默认权重、方向词典、标签策略。"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "pool.db"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# 真实数据校准的初始权重（15 条作品，Spearman 相关归一化）
DEFAULT_WEIGHTS = {
    "finish": 0.3544,    # 完播率
    "ret2s": 0.2970,     # 2s 留存率 = 1 - 2s跳出率
    "share": 0.2841,     # 分享率
    "fav": 0.0328,       # 收藏率
    "comment": 0.0318,   # 评论率
}

WEIGHT_LABELS = {
    "finish": "完播率",
    "ret2s": "2s 留存率",
    "share": "分享率",
    "fav": "收藏率",
    "comment": "评论率",
}

# 权重自动重算的样本门槛：低于此条数沿用默认值，避免小样本噪声
MIN_SAMPLES_FOR_AUTO_WEIGHT = 10

# 时间衰减半衰期（天）：近期作品对权重影响更大
WEIGHT_HALF_LIFE_DAYS = 30

# 赛道方向词典（DeepSeek 从此列表中选择，避免自由生成导致分组碎片化）
DEFAULT_DIRECTIONS = [
    "实战求教",
    "技术判断",
    "级别讨论",
    "动作展示",
    "运营吐槽",
    "装备测评",
    "球馆日常",
    "赛事热点",
    "其他",
]

# 每日候选数量与文案长度
CANDIDATES_PER_DAY = 2
SCRIPT_MAX_CHARS = 40   # 约 10 秒口播
TAGS_PER_CANDIDATE = 3  # 赛道词 + 内容类型词 + 热点词
DEDUP_WINDOW_DAYS = 7
DEDUP_SIMILARITY = 0.62  # 与近期文案的相似度上限，超过则弃用重写

# 生成多样性
DEFAULT_TEMPERATURE = 1.3  # DeepSeek 采样温度，越高用词语气越发散
MIN_SAMPLES_FOR_DIRECTION = 2  # 方向进入候选池的最低样本数（原为 3，放宽以引入探索方向）

DEFAULT_PUSH_TIME = "20:00"
DEFAULT_DEEPSEEK_BASE = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"


def env_or(key: str, default: str) -> str:
    return os.environ.get(key, default)
