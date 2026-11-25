"""动态权重引擎：全量数据 + 时间衰减，作品数据变动即自动重算。

方法：加权 Spearman 秩相关（小样本稳健）取绝对值归一化。
目标变量 = log(播放量)，代表"进池结果"；播放量本身不作为权重特征。
时间衰减：近期作品权重大，半衰期可配置（默认 30 天）。
数据成熟度：发布未满 24h 的作品降权，避免未稳数据带偏。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from . import db
from .config import (
    DEFAULT_WEIGHTS,
    MIN_SAMPLES_FOR_AUTO_WEIGHT,
    MIN_SAMPLES_FOR_DIRECTION,
    WEIGHT_HALF_LIFE_DAYS,
    WEIGHT_LABELS,
)

SIGNAL_KEYS = ["finish", "ret2s", "share", "fav", "comment"]
SIGNAL_LABELS = WEIGHT_LABELS


@dataclass
class DirectionScore:
    direction: str
    score: float
    n: int
    avg_views: int
    reliable: bool
    note: str = ""


@dataclass
class EngineResult:
    weights: dict[str, float]
    correlations: dict[str, dict[str, float]]
    sample_size: int
    source: str            # auto / default / locked
    directions: list[DirectionScore]
    data_gap_note: str
    last_change: str


def rankdata(xs: list[float]) -> list[float]:
    """平均秩，处理并列值。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    ranks = [0.0] * len(xs)
    i = 0
    while i < len(xs):
        j = i + 1
        while j < len(xs) and xs[order[j]] == xs[order[i]]:
            j += 1
        avg = (i + 1 + j) / 2.0
        for k in range(i, j):
            ranks[order[k]] = avg
        i = j
    return ranks


def weighted_pearson(xs: list[float], ys: list[float], ws: list[float]) -> float:
    total_w = sum(ws)
    if total_w == 0:
        return 0.0
    mx = sum(w * x for w, x in zip(ws, xs)) / total_w
    my = sum(w * y for w, y in zip(ws, ys)) / total_w
    num = sum(w * (x - mx) * (y - my) for w, x, y in zip(ws, xs, ys))
    dx = math.sqrt(sum(w * (x - mx) ** 2 for w, x in zip(ws, xs)))
    dy = math.sqrt(sum(w * (y - my) ** 2 for w, y in zip(ws, ys)))
    return num / (dx * dy) if dx and dy else 0.0


def weighted_spearman(xs: list[float], ys: list[float], ws: list[float]) -> float:
    return weighted_pearson(rankdata(xs), rankdata(ys), ws)


def _parse_date(s: str) -> date:
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return date.today()


def _sample_weights(works: list[dict[str, Any]], today: date | None = None) -> list[float]:
    """时间衰减 + 数据成熟度加权。"""
    if today is None:
        today = date.today()
    half_life = float(db.get_setting("weight_half_life", str(WEIGHT_HALF_LIFE_DAYS)))
    half_life = max(1.0, half_life)
    out: list[float] = []
    for w in works:
        age = max(0, (today - _parse_date(w["publish_date"])).days)
        decay = 0.5 ** (age / half_life)
        if age < 1:
            decay *= 0.3   # 未满 24h，数据未稳
        out.append(decay)
    return out


def get_current_weights() -> dict[str, float]:
    raw = db.get_setting("weights", "")
    try:
        loaded = json.loads(raw) if raw else {}
        out = {k: float(loaded.get(k, DEFAULT_WEIGHTS[k])) for k in SIGNAL_KEYS}
    except (ValueError, TypeError, json.JSONDecodeError):
        out = dict(DEFAULT_WEIGHTS)
    total = sum(out.values())
    if total <= 0:
        return dict(DEFAULT_WEIGHTS)
    return {k: v / total for k, v in out.items()}


def is_locked() -> bool:
    return db.get_setting("weights_locked", "0") not in ("0", "false", "False", "")


def compute_weights(trigger: str = "auto", today: date | None = None) -> EngineResult:
    """计算（或沿用它）权重，返回完整引擎结果。"""
    works = db.list_works()
    sample_n = len(works)
    if today is None:
        today = date.today()

    locked = is_locked()
    if locked:
        weights = get_current_weights()
        source = "locked"
        correlations: dict[str, dict[str, float]] = {}
    elif sample_n < MIN_SAMPLES_FOR_AUTO_WEIGHT:
        weights = dict(DEFAULT_WEIGHTS)
        source = "default"
        correlations = {}
    else:
        sw = _sample_weights(works, today)
        target = [math.log(max(1, w["views"])) for w in works]
        signals: dict[str, list[float]] = {
            "finish": [float(w["finish"]) for w in works],
            "ret2s": [float(w["ret2s"]) for w in works],
            "share": [float(w["shares"]) / max(1, w["views"]) for w in works],
            "fav": [float(w["favs"]) / max(1, w["views"]) for w in works],
            "comment": [float(w["comments"]) / max(1, w["views"]) for w in works],
        }
        correlations = {
            k: {"spearman": round(weighted_spearman(v, target, sw), 4)}
            for k, v in signals.items()
        }
        abs_corr = {k: abs(correlations[k]["spearman"]) for k in SIGNAL_KEYS}
        total = sum(abs_corr.values())
        weights = (
            {k: abs_corr[k] / total for k in SIGNAL_KEYS}
            if total > 0 else dict(DEFAULT_WEIGHTS)
        )
        source = "auto"
        db.set_setting("weights", json.dumps({k: round(v, 4) for k, v in weights.items()}))
        db.add_weight_history(weights, sample_n, trigger, "自动重算（加权 Spearman）")

    # 方向得分：用当前权重对每条作品打分，再按方向加权平均
    dirs = _direction_scores(works, weights, today)

    history = db.list_weight_history(limit=1)
    last_change = history[0]["changed_at"] if history else "尚未重算"

    return EngineResult(
        weights=weights,
        correlations=correlations,
        sample_size=sample_n,
        source=source,
        directions=dirs,
        data_gap_note=(
            "无完播率接口缺口已消除（后台手工录入）。"
            "点赞/弹幕记录备查不进权重；垂直匹配因全为羽毛球内容无方差，未纳入。"
        ),
        last_change=last_change,
    )


def _direction_scores(works: list[dict[str, Any]], weights: dict[str, float],
                      today: date) -> list[DirectionScore]:
    """按方向聚合：先给每条作品按当前权重算质量分，再加权平均。"""
    groups: dict[str, list[tuple[dict[str, Any], float]]] = {}
    for w in works:
        d = (w.get("direction") or "其他").strip() or "其他"
        views = max(1, int(w["views"]))
        normalized = {
            "finish": float(w["finish"]),
            "ret2s": float(w["ret2s"]),
            "share": min(1.0, float(w["shares"]) / views * 50),
            "fav": min(1.0, float(w["favs"]) / views * 50),
            "comment": min(1.0, float(w["comments"]) / views * 10),
        }
        quality = sum(weights.get(k, 0) * normalized[k] for k in SIGNAL_KEYS)
        groups.setdefault(d, []).append((w, quality))

    out: list[DirectionScore] = []
    for d, items in groups.items():
        n = len(items)
        avg_views = round(sum(int(w["views"]) for w, _ in items) / n)
        raw_score = sum(q for _, q in items) / n
        # 置信度修正：样本少的方向降权，避免一条爆款带偏
        confidence = n / (n + 3)
        score = round(raw_score * confidence, 4)
        reliable = n >= MIN_SAMPLES_FOR_DIRECTION
        out.append(DirectionScore(
            direction=d,
            score=score,
            n=n,
            avg_views=avg_views,
            reliable=reliable,
            note="" if reliable else f"样本不足 {MIN_SAMPLES_FOR_DIRECTION} 条，仅供参考",
        ))
    out.sort(key=lambda x: -x.score)
    return out


def reliable_directions(result: EngineResult) -> list[DirectionScore]:
    return [d for d in result.directions if d.reliable]
