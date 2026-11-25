"""DeepSeek 文案生成器：结合方向得分、历史钩子、热点，生成每日两条候选。

无模板兜底：未配置 API Key 时明确报错，不生成假文案。
输出严格 JSON 校验；字数、标签数、近 7 天去重均在此处理。
"""

from __future__ import annotations

import json
import logging
import re
from difflib import SequenceMatcher
from typing import Any

import requests

from . import db
from .config import (
    CANDIDATES_PER_DAY,
    DEDUP_SIMILARITY,
    DEDUP_WINDOW_DAYS,
    DEFAULT_TEMPERATURE,
    SCRIPT_MAX_CHARS,
    TAGS_PER_CANDIDATE,
)
from .weight_engine import DirectionScore, EngineResult

log = logging.getLogger("generator")


class GenerationError(Exception):
    """生成失败，错误信息可直接展示给用户。"""


def _api_config() -> tuple[str, str, str, float]:
    key = db.get_setting("deepseek_api_key", "").strip()
    base = db.get_setting("deepseek_base", "https://api.deepseek.com").strip().rstrip("/")
    model = db.get_setting("deepseek_model", "deepseek-chat").strip()
    try:
        temperature = float(db.get_setting("temperature", str(DEFAULT_TEMPERATURE)))
    except (ValueError, TypeError):
        temperature = DEFAULT_TEMPERATURE
    temperature = max(0.1, min(2.0, temperature))
    if not key:
        raise GenerationError("未配置 DeepSeek API Key，请到推送配置页填写后再生成。")
    return key, base, model, temperature


def _directions() -> list[str]:
    try:
        dirs = json.loads(db.get_setting("directions", "[]"))
        return [d for d in dirs if isinstance(d, str) and d.strip()]
    except (ValueError, TypeError, json.JSONDecodeError):
        return ["其他"]


def _top_scripts(direction: str, limit: int = 4) -> list[dict[str, Any]]:
    """取该方向高表现作品的文案作为风格参考（按播放量排序）。"""
    works = [w for w in db.list_works() if (w.get("direction") or "") == direction]
    works.sort(key=lambda w: -int(w["views"]))
    return [
        {"script": w["script"], "views": w["views"],
         "finish": w["finish"], "ret2s": w["ret2s"]}
        for w in works[:limit]
    ]


def _call_deepseek(prompt: str) -> str:
    key, base, model, temperature = _api_config()
    try:
        resp = requests.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": "你是抖音羽毛球赛道内容策划。严格按用户要求输出 JSON，不要输出任何多余文字。"},
                    {"role": "user", "content": prompt},
                ],
                "temperature": temperature,
                "max_tokens": 1200,
                "response_format": {"type": "json_object"},
            },
            timeout=60,
        )
    except requests.RequestException as exc:
        raise GenerationError(f"DeepSeek 请求失败：{exc}") from exc

    if resp.status_code == 401:
        raise GenerationError("DeepSeek API Key 无效（401），请检查配置。")
    if resp.status_code == 429:
        raise GenerationError("DeepSeek 请求过于频繁或余额不足（429），请稍后重试。")
    if resp.status_code >= 500:
        raise GenerationError(f"DeepSeek 服务异常（{resp.status_code}），请稍后重试。")
    if resp.status_code != 200:
        raise GenerationError(f"DeepSeek 返回异常（{resp.status_code}）：{resp.text[:200]}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise GenerationError(f"DeepSeek 返回格式无法解析：{exc}") from exc


def _extract_json(text: str) -> Any:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", text)
        if m:
            try:
                return json.loads(m.group())
            except json.JSONDecodeError:
                pass
    raise GenerationError("DeepSeek 输出不是合法 JSON，请重试。")


def _short_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _too_similar(script: str) -> bool:
    """与近 7 天候选去重；阈值收紧后更容易触发重写。"""
    for c in db.recent_candidates(DEDUP_WINDOW_DAYS):
        if _short_similarity(script, c["script"]) > DEDUP_SIMILARITY:
            return True
    return False


def build_prompt(slot: int, direction: DirectionScore, hotspots: list[dict[str, str]],
                 max_chars: int, tag_count: int, avoid: list[str],
                 first_script: str = "") -> str:
    refs = _top_scripts(direction.direction, limit=4)
    ref_text = "\n".join(
        f"- [{r['views']}播放] {r['script']}"
        for r in refs
    ) or "- 暂无该方向历史作品"
    hot_text = "、".join(h["keyword"] for h in hotspots[:5]) if hotspots else "今日无可用热点"
    avoid_list = list(avoid)
    if first_script:
        avoid_list.append(first_script)
    avoid_text = "\n".join(f"- {s}" for s in avoid_list[-12:]) if avoid_list else "无"

    if slot == 0:
        strategy = (
            "延续该方向（方向可以和历史一致），但表达必须明显不同。"
            "只从历史文案里提炼「为什么有效」的结构逻辑，"
            "严禁复用它的开头、句式、关键词和结尾引导语。"
        )
    else:
        strategy = (
            "探索位：换一个与第一条完全不同的切入角度和钩子结构。"
            "优先把当日热点转译成业余球友能共鸣的场景。"
        )

    hot_rule = (
        "当日热点多为职业赛事新闻。不要直接播报赛事结果，"
        "必须转译成业余球友日常能共鸣的角度"
        "（例如：从职业选手的处理里学一个业余也能用的技巧）。"
        if hotspots else
        "今日没有可用热点，就从该方向里换一个历史没用过的具体场景切入。"
    )

    return f"""你要为一位羽毛球日常赛道抖音博主写 1 条短视频文案。

【本次选题方向】{direction.direction}（方向得分 {direction.score}，历史样本 {direction.n} 条）
【本次角色】{"第 1 条 · 保守位" if slot == 0 else "第 2 条 · 探索位"}
【策略】{strategy}
【热点使用规则】{hot_rule}

【该方向历史文案 —— 只学结构逻辑，禁止复制表达】
{ref_text}

【当日热点】{hot_text}

【必须避开的文案（不得以相似句式或开头改写）】
{avoid_text}

【硬性要求】
1. 完整口播文案不超过 {max_chars} 个字（约 10 秒），必须是能直接念出来的完整句子。
2. 保持账号语气：口语化、第一人称"主包"、有真实情绪、结尾自然引导互动。但句式、开头、用词必须与上方所有文案明显不同。
3. 开头第一句不得与任何参考文案/避开文案的前 8 个字相同；禁止使用"你是不是也这样""别再瞎练了"这类通用模板开头。
4. 不得改写、续写或拼接上方任何一条文案，必须是一个全新的具体场景。
5. 标签严格 {tag_count} 个：1 个赛道大词（如 #羽毛球）+ 1 个内容类型词（如 #羽毛球教学 / #羽毛球日常）+ 1 个热点或具体技术词。
6. 标题不超过 20 字，能引发好奇或共鸣。
7. 不要编造具体人物身份、赛事结果、数据。不要使用 emoji。

【输出格式】只输出一个 JSON 对象，不要任何解释文字：
{{"direction":"{direction.direction}","title":"标题","script":"完整口播文案","tags":["标签1","标签2","标签3"]}}
"""


def _select_directions(engine: EngineResult, count: int,
                       recent_rows: list[dict[str, Any]]):
    """保守位 + 探索位的方向选择。预览与正式生成共用，保证展示即实际。"""
    dirs = [d for d in engine.directions if d.reliable]
    if not dirs:
        raise GenerationError("可靠的方向得分不足（每方向需至少 2 条作品数据），无法生成候选。")
    recent = [c["script"] for c in recent_rows]
    used_directions = {c["direction"] for c in recent_rows}
    selected = [dirs[0]]
    rest = dirs[1:]
    fresh_names = {d.direction for d in rest if d.direction not in used_directions}
    if count > 1:
        if rest:
            pool = [d for d in rest if d.direction in fresh_names] or rest
            selected.append(pool[0])
        else:
            selected.append(dirs[0])
    return selected[:count], recent, fresh_names


def preview_prompts(engine: EngineResult) -> dict[str, Any]:
    """预览当前配置下实际会发给 DeepSeek 的两条提示词（只读，不调用模型）。"""
    today = db.today_str()
    count = int(db.get_setting("candidates_per_day", str(CANDIDATES_PER_DAY)))
    recent_rows = db.recent_candidates(DEDUP_WINDOW_DAYS)
    top, recent, _ = _select_directions(engine, count, recent_rows)
    hotspots = [{"keyword": h["keyword"]} for h in db.hotspots_for_date(today)]
    max_chars = int(db.get_setting("script_max_chars", str(SCRIPT_MAX_CHARS)))
    tag_count = int(db.get_setting("tags_per_candidate", str(TAGS_PER_CANDIDATE)))
    existing = db.candidates_for_date(today)
    first_script = existing[0]["script"] if existing else ""

    prompts = []
    for slot, direction in enumerate(top):
        prompts.append({
            "slot": slot + 1,
            "role": "保守位" if slot == 0 else "探索位",
            "direction": direction.direction,
            "text": build_prompt(
                slot, direction, hotspots, max_chars, tag_count, recent,
                first_script if slot > 0 else "",
            ),
        })
    try:
        temperature = float(db.get_setting("temperature", str(DEFAULT_TEMPERATURE)))
    except (ValueError, TypeError):
        temperature = DEFAULT_TEMPERATURE
    return {
        "prompts": prompts,
        "hotspot_count": len(hotspots),
        "avoid_count": len(recent) + (1 if first_script else 0),
        "max_chars": max_chars,
        "tag_count": tag_count,
        "temperature": temperature,
    }


def generate_candidates(engine: EngineResult, force: bool = False) -> list[dict[str, Any]]:
    """生成当日候选。若今日已有候选则直接返回；force=True 清空草稿重新生成。"""
    today = db.today_str()
    existing_drafts = [
        c for c in db.candidates_for_date(today) if c["status"] == "draft"
    ]
    if not force and len(existing_drafts) >= int(
        db.get_setting("candidates_per_day", str(CANDIDATES_PER_DAY))
    ):
        return db.candidates_for_date(today)

    count = int(db.get_setting("candidates_per_day", str(CANDIDATES_PER_DAY)))
    recent_rows = db.recent_candidates(DEDUP_WINDOW_DAYS)
    top, recent, fresh_names = _select_directions(engine, count, recent_rows)

    hotspots = [
        {"keyword": h["keyword"]} for h in db.hotspots_for_date(today)
    ]
    max_chars = int(db.get_setting("script_max_chars", str(SCRIPT_MAX_CHARS)))
    tag_count = int(db.get_setting("tags_per_candidate", str(TAGS_PER_CANDIDATE)))

    results: list[dict[str, Any]] = []
    first_script = ""
    for slot, direction in enumerate(top):
        prompt = build_prompt(slot, direction, hotspots, max_chars, tag_count,
                              recent, first_script)
        raw = _call_deepseek(prompt)
        obj = _extract_json(raw)
        script = str(obj.get("script", "")).strip()
        title = str(obj.get("title", "")).strip()
        tags = obj.get("tags", [])
        if not isinstance(tags, list):
            raise GenerationError(f"第 {slot + 1} 条候选标签格式错误，请重试。")
        tags = [str(t).strip().lstrip("#") for t in tags][:tag_count]
        tags = [t for t in tags if t]

        if len(script) > max_chars:
            raise GenerationError(
                f"第 {slot + 1} 条文案 {len(script)} 字，超过 {max_chars} 字限制，请重试。"
            )
        if len(tags) < tag_count:
            raise GenerationError(f"第 {slot + 1} 条标签不足 {tag_count} 个，请重试。")
        if _too_similar(script):
            raise GenerationError(f"第 {slot + 1} 条与近期文案过于相似，请重试。")
        if first_script and _short_similarity(script, first_script) > DEDUP_SIMILARITY:
            raise GenerationError("第 2 条与第 1 条过于相似，请重试。")

        if slot == 0:
            rationale = (
                f"保守位 · 方向得分 {direction.score}（{direction.n} 条样本）；"
                "方向延续，表达全新"
            )
        else:
            novelty = "近期未用方向" if direction.direction in fresh_names else "换角度探索"
            rationale = (
                f"探索位 · 方向得分 {direction.score}（{direction.n} 条样本）；"
                f"{novelty}，热点转业余视角"
            )
        results.append({
            "slot": slot + 1, "direction": direction.direction,
            "title": title[:20], "script": script,
            "tags": tags, "rationale": rationale, "score": direction.score,
        })
        recent.append(script)
        if slot == 0:
            first_script = script

    # 两条都通过校验后才落库：生成失败时不会破坏当天已有候选
    if force:
        db.clear_draft_candidates(today)
    for r in results:
        r["id"] = db.add_candidate({
            "gen_date": today, "slot": r["slot"],
            "direction": r["direction"], "title": r["title"],
            "script": r["script"],
            "tags": json.dumps(r["tags"], ensure_ascii=False),
            "rationale": r["rationale"], "score": r["score"],
            "status": "draft",
        })
    return results
