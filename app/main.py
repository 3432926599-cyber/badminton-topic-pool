"""FastAPI 主程序：4 个页面 + 数据 API + 动态权重触发。"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import crawler, db, generator, mailer, scheduler
from .config import STATIC_DIR, TEMPLATES_DIR, WEIGHT_LABELS
from .weight_engine import compute_weights

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start_scheduler()
    yield


app = FastAPI(title="羽毛球选题数据池", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _from_json(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


templates.env.filters["fromjson"] = _from_json


def render(request: Request, name: str, active: str, **ctx: Any):
    base = {"request": request, "active": active}
    base.update(ctx)
    return templates.TemplateResponse(request, name, base)


# ---------------- 页面 ----------------

@app.get("/")
def dashboard(request: Request):
    engine = compute_weights()
    today = db.today_str()
    candidates = db.candidates_for_date(today)
    if not candidates:
        candidates = db.candidates_for_date(
            (date.today() - timedelta(days=1)).isoformat()
        )
    works = db.list_works()
    by_date: dict[str, list[int]] = {}
    for w in works:
        d = w["publish_date"][:10]
        by_date.setdefault(d, []).append(int(w["views"]))
    trends = [
        {"date": d, "views": round(sum(v) / len(v))}
        for d, v in sorted(by_date.items())[-14:]
    ]
    max_views = max((t["views"] for t in trends), default=1) or 1
    return render(
        request, "dashboard.html", "dashboard",
        candidates=candidates,
        trends=trends,
        max_views=max_views,
        engine=engine,
        weight_labels=WEIGHT_LABELS,
        push_time=db.get_setting("push_time", "20:00"),
    )


@app.get("/works")
def works_page(request: Request):
    works = [dict(w) for w in db.list_works()]
    return render(request, "works.html", "works", works=works)


@app.get("/hotspots")
def hotspots_page(request: Request):
    return render(request, "hotspots.html", "hotspots", hotspots=db.list_hotspots())


@app.get("/settings")
def settings_page(request: Request):
    s = {k: db.get_setting(k, "") for k in [
        "push_time", "smtp_host", "smtp_port", "smtp_user", "mail_from",
        "mail_to", "smtp_use_ssl", "deepseek_base", "deepseek_model",
        "candidates_per_day", "script_max_chars", "tags_per_candidate",
        "temperature", "weight_half_life", "weights_locked", "directions",
    ]}
    engine = compute_weights()
    try:
        prompt_preview = generator.preview_prompts(engine)
    except generator.GenerationError as exc:
        prompt_preview = {"prompts": [], "error": str(exc)}
    return render(
        request, "settings.html", "settings", s=s, engine=engine,
        weight_labels=WEIGHT_LABELS,
        weight_history=db.list_weight_history(limit=15),
        api_key_set=bool(db.get_setting("deepseek_api_key", "").strip()),
        smtp_password_set=bool(db.get_setting("smtp_password", "").strip()),
        prompt_preview=prompt_preview,
    )


# ---------------- 作品 API：变动即触发权重重算 ----------------

def _normalize_work(payload: dict[str, Any]) -> dict[str, Any]:
    finish = float(payload.get("finish", 0))
    jump2 = float(payload.get("jump2", 0))
    if finish > 1:
        finish /= 100
    if jump2 > 1:
        jump2 /= 100
    finish = max(0.0, min(1.0, finish))
    jump2 = max(0.0, min(1.0, jump2))
    return {
        "publish_date": str(payload.get("publish_date") or date.today().isoformat()),
        "script": str(payload.get("script") or "").strip(),
        "title": str(payload.get("title") or "").strip(),
        "direction": str(payload.get("direction") or "其他").strip(),
        "tags": str(payload.get("tags") or "").strip(),
        "views": int(payload.get("views") or 0),
        "likes": int(payload.get("likes") or 0),
        "comments": int(payload.get("comments") or 0),
        "shares": int(payload.get("shares") or 0),
        "favs": int(payload.get("favs") or 0),
        "danmu": int(payload.get("danmu") or 0),
        "finish": finish,
        "jump2": jump2,
        "ret2s": 1 - jump2,
        "note": str(payload.get("note") or "").strip(),
    }


def _is_duplicate(data: dict[str, Any]) -> bool:
    for w in db.list_works():
        if w["publish_date"] == data["publish_date"] and w["script"] == data["script"]:
            return True
    return False


@app.post("/api/works")
async def api_add_work(payload: dict[str, Any]):
    data = _normalize_work(payload)
    if not data["script"]:
        return JSONResponse({"ok": False, "error": "文案全文不能为空"}, status_code=400)
    if data["views"] <= 0:
        return JSONResponse({"ok": False, "error": "播放量必须大于 0"}, status_code=400)
    if _is_duplicate(data):
        return JSONResponse({"ok": False, "error": "该日期与文案的组合已存在，未重复录入"}, status_code=400)
    wid = db.add_work(data)
    engine = compute_weights(trigger="auto")
    return {
        "ok": True, "id": wid,
        "weights": {WEIGHT_LABELS.get(k, k): round(v, 4) for k, v in engine.weights.items()},
        "weight_source": engine.source,
        "sample_size": engine.sample_size,
    }


@app.put("/api/works/{work_id}")
async def api_update_work(work_id: int, payload: dict[str, Any]):
    data = _normalize_work(payload)
    ok = db.update_work(work_id, data)
    if not ok:
        return JSONResponse({"ok": False, "error": "作品不存在"}, status_code=404)
    engine = compute_weights(trigger="auto")
    return {
        "ok": True,
        "weights": {WEIGHT_LABELS.get(k, k): round(v, 4) for k, v in engine.weights.items()},
        "weight_source": engine.source,
    }


@app.delete("/api/works/{work_id}")
async def api_delete_work(work_id: int):
    ok = db.delete_work(work_id)
    if not ok:
        return JSONResponse({"ok": False, "error": "作品不存在"}, status_code=404)
    engine = compute_weights(trigger="auto")
    return {"ok": True, "sample_size": engine.sample_size, "weight_source": engine.source}


# ---------------- 引擎与生成 ----------------

@app.get("/api/engine")
def api_engine():
    e = compute_weights()
    return {
        "weights": e.weights,
        "correlations": e.correlations,
        "sample_size": e.sample_size,
        "source": e.source,
        "last_change": e.last_change,
        "directions": [
            {
                "direction": d.direction, "score": d.score, "n": d.n,
                "avg_views": d.avg_views, "reliable": d.reliable, "note": d.note,
            }
            for d in e.directions
        ],
        "data_gap_note": e.data_gap_note,
    }


@app.post("/api/recompute")
async def api_recompute():
    e = compute_weights(trigger="manual")
    return {"ok": True, "weights": e.weights, "sample_size": e.sample_size, "source": e.source}


@app.post("/api/crawl")
async def api_crawl():
    return crawler.crawl_and_store()


@app.post("/api/generate")
async def api_generate(payload: dict[str, Any] | None = None):
    payload = payload or {}
    force = payload.get("force") in (True, 1, "1", "true", "True")
    e = compute_weights()
    try:
        candidates = generator.generate_candidates(e, force=force)
        return {"ok": True, "candidates": candidates}
    except generator.GenerationError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)


@app.post("/api/send")
async def api_send():
    today = db.today_str()
    candidates = db.candidates_for_date(today)
    if not candidates:
        return JSONResponse({"ok": False, "error": "今日还没有候选，请先生成"}, status_code=400)
    ok, msg = mailer.send_daily(candidates)
    return {"ok": ok, "message": msg}


@app.post("/api/daily")
async def api_daily():
    return scheduler.daily_job()


@app.post("/api/candidates/{cid}/status")
async def api_candidate_status(cid: int, payload: dict[str, Any]):
    status = str(payload.get("status", ""))
    if status not in ("draft", "adopted", "discarded"):
        return JSONResponse({"ok": False, "error": "状态无效"}, status_code=400)
    db.set_candidate_status(cid, status)
    return {"ok": True}


# ---------------- 配置 API ----------------

@app.post("/api/settings")
async def api_settings(payload: dict[str, Any]):
    writable = [
        "push_time", "smtp_host", "smtp_port", "smtp_user", "mail_from",
        "mail_to", "smtp_use_ssl", "deepseek_base", "deepseek_model",
        "candidates_per_day", "script_max_chars", "tags_per_candidate",
        "temperature", "weight_half_life",
    ]
    mapping = {k: str(payload[k]) for k in writable if k in payload}
    for k in ("deepseek_api_key", "smtp_password"):
        if payload.get(k):
            mapping[k] = str(payload[k])
    if "weights_locked" in payload:
        mapping["weights_locked"] = "1" if payload["weights_locked"] in (True, 1, "1", "true") else "0"
    if "directions" in payload:
        dirs = payload["directions"]
        if isinstance(dirs, str):
            dirs = [x.strip() for x in dirs.replace("，", ",").split(",") if x.strip()]
        mapping["directions"] = json.dumps(dirs, ensure_ascii=False)
    db.set_settings(mapping)
    return {"ok": True, "updated": list(mapping.keys())}
