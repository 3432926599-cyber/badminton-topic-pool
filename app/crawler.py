"""羽毛球赛道热点爬虫：虎扑垂直主源 + 头条热榜补充，关键词过滤，抓不到只记日志不编造。

已移除的稳定失效源（2026-11 实测）：
- 百度新闻 RSS：返回安全验证页
- 搜狗微信：反爬拦截
- tophub.today：403
- 微博热搜 / 知乎热榜：403 / 401
"""

from __future__ import annotations

import logging
import re
from typing import Callable

import requests
from bs4 import BeautifulSoup

from . import db

log = logging.getLogger("crawler")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 羽毛球赛道关键词：命中任一才入库
NICHE_WORDS = [
    "羽毛球", "羽球", "苏迪曼", "尤伯杯", "汤尤杯", "世锦赛", "中羽",
    "杀球", "高远球", "吊球", "搓球", "架拍", "反手", "前场", "后场",
    "羽毛球拍", "球馆", "羽联", "林丹", "李宗伟", "桃田", "安赛龙",
    "国羽", "混双", "女单", "男单", "男双", "女双", "周昊东", "陈雨菲",
]


def _is_niche(text: str) -> bool:
    return any(w in text for w in NICHE_WORDS)


def _clean(title: str) -> str:
    """去掉来源前缀和空白，保留可读标题。"""
    title = re.sub(r"^\[[^\]]{1,8}\]\s*", "", title.strip())
    return re.sub(r"\s+", " ", title).strip()


def source_hupu_badminton() -> list[dict]:
    """虎扑羽毛球版块：垂直主源，每日数十条真实帖子标题。"""
    items: list[dict] = []
    try:
        resp = requests.get("https://bbs.hupu.com/yumaoqiu",
                            headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select(".p-title a") or [
            a for a in soup.select("a")
            if a.get("href", "").endswith(".html") and _is_niche(a.get_text(" ", strip=True))
        ]
        seen: set[str] = set()
        for idx, a in enumerate(links[:40]):
            title = _clean(a.get_text(" ", strip=True))
            if len(title) < 6 or not _is_niche(title) or title in seen:
                continue
            seen.add(title)
            href = a.get("href", "")
            url = ("https://bbs.hupu.com" + href) if href.startswith("/") else href
            items.append({
                "keyword": title[:80],
                "heat": float(max(1, 30 - idx)),  # 列表序作为相对热度
                "source": "虎扑羽毛球",
                "url": url,
            })
        log.info("虎扑羽毛球抓取 %d 条", len(items))
    except Exception as exc:  # noqa: BLE001
        log.warning("虎扑羽毛球抓取失败: %s", exc)
    return items[:30]


def source_toutiao_hot() -> list[dict]:
    """今日头条热榜：结构化 JSON，带真实热度值，命中羽毛球词时入库。"""
    items: list[dict] = []
    try:
        resp = requests.get(
            "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
            headers=HEADERS, timeout=15,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])
        for row in rows:
            title = _clean(str(row.get("Title", "")))
            if not title or not _is_niche(title):
                continue
            hot = float(row.get("HotValue") or 0)
            items.append({
                "keyword": title[:80],
                "heat": round(min(30.0, hot / 2_000_000), 2),  # 归一到与虎扑同量级
                "source": "头条热榜",
                "url": str(row.get("Url") or ""),
            })
        log.info("头条热榜命中羽毛球 %d 条", len(items))
    except Exception as exc:  # noqa: BLE001
        log.warning("头条热榜抓取失败: %s", exc)
    return items


def source_baidu_top() -> list[dict]:
    """百度热搜官方公开页，从内嵌 JSON 提取词条（兜底源）。"""
    items: list[dict] = []
    try:
        resp = requests.get("https://top.baidu.com/board?tab=realtime",
                            headers=HEADERS, timeout=12)
        resp.raise_for_status()
        words = re.findall(r'"word":"([^"]+)"', resp.text)
        words += [m.get_text(" ", strip=True) for m in
                  BeautifulSoup(resp.text, "html.parser").select(".c-single-text-ellipsis")]
        seen = set()
        for w in words:
            w = _clean(w)
            if not w or w in seen or not _is_niche(w):
                continue
            seen.add(w)
            items.append({"keyword": w, "heat": 0, "source": "百度热搜", "url": ""})
    except Exception as exc:  # noqa: BLE001
        log.warning("百度热搜抓取失败: %s", exc)
    return items


SOURCES: list[Callable[[], list[dict]]] = [
    source_hupu_badminton,
    source_toutiao_hot,
    source_baidu_top,
]


def crawl_and_store() -> dict:
    """抓取所有源，过滤后写入热点池；抓不到返回 0，不编造。"""
    today = db.today_str()
    collected = 0
    by_source: dict[str, int] = {}
    for src in SOURCES:
        try:
            items = src()
        except Exception as exc:  # noqa: BLE001
            log.warning("源 %s 异常: %s", src.__name__, exc)
            items = []
        added = 0
        for item in items:
            if db.upsert_hotspot(
                today, item["keyword"], item.get("heat", 0),
                item.get("source", ""), item.get("url", ""),
            ):
                added += 1
        collected += added
        by_source[src.__name__] = added
    return {"date": today, "collected": collected, "by_source": by_source}
