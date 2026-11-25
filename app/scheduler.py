"""定时调度：每日 push_time 执行抓热点 → 算权重 → 生成候选 → 发邮件。"""

from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import crawler, db, generator, mailer
from .weight_engine import compute_weights

log = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None


def daily_job() -> dict:
    """每日完整流程，也可从网页手动触发。"""
    log.info("daily job start")
    crawl = crawler.crawl_and_store()
    engine = compute_weights(trigger="auto")
    try:
        candidates = generator.generate_candidates(engine)
    except generator.GenerationError as exc:
        log.warning("生成失败: %s", exc)
        return {"ok": False, "error": str(exc), "crawl": crawl}
    ok, mail_msg = mailer.send_daily(candidates)
    log.info("daily job done: %s, mail=%s", len(candidates), ok)
    return {
        "ok": True,
        "candidates": len(candidates),
        "crawl": crawl,
        "mail_sent": ok,
        "mail_message": mail_msg,
    }


def start_scheduler() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None:
        return _scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Shanghai")

    def refresh_job() -> None:
        t = db.get_setting("push_time", "20:00")
        try:
            hour, minute = (int(x) for x in t.split(":")[:2])
        except (ValueError, TypeError):
            hour, minute = 20, 0
        _scheduler.remove_all_jobs()
        _scheduler.add_job(daily_job, CronTrigger(hour=hour, minute=minute), id="daily")
        log.info("定时任务已设置为每日 %02d:%02d", hour, minute)

    refresh_job()
    _scheduler.add_job(refresh_job, CronTrigger(minute=1), id="refresh")
    _scheduler.start()
    return _scheduler
