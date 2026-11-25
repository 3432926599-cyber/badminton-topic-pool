# 羽毛球选题数据池 · Badminton Topic Pool

A self-hosted content-ideation tool for badminton short-video creators. It crawls daily hot topics, scores them with a dynamic weight engine, uses DeepSeek to generate two candidate scripts, and emails them to you on a schedule.

一个自托管的羽毛球短视频选题工具：每日抓热点 → 算权重 → DeepSeek 生成两条候选文案 → 定时邮件推送。

## What it does · 功能

| Page · 页面 | What it does · 作用 |
|---|---|
| **Dashboard · 首页看板** | Today's two candidate scripts, recent performance trend, current weights, quick links, and manual action buttons |
| **Works · 作品录入** | Manually record 8 fields per video; weights auto-recalculate on every create/edit/delete |
| **Hotspots · 热点列表** | Read-only list of hot topics written by the crawler |
| **Settings · 推送配置** | SMTP, push time, DeepSeek API, weight parameters, direction dictionary; secrets are stored locally only and never echoed back |

## Tech stack · 技术栈

Python 3.13 · FastAPI · SQLite · Jinja2 · APScheduler · BeautifulSoup4 · vanilla HTML/CSS/JS (zero CDN).

## Project structure · 目录结构

```
badminton-topic-pool/
├── app/
│   ├── main.py            # FastAPI app + pages + API routes
│   ├── db.py              # SQLite data layer (works / hotspots / candidates / settings / history)
│   ├── config.py          # defaults, initial weights, direction dictionary
│   ├── weight_engine.py   # dynamic weight engine (core)
│   ├── generator.py       # DeepSeek candidate generation + dedup
│   ├── crawler.py         # hotspot crawler
│   ├── mailer.py          # SMTP sender
│   └── scheduler.py       # APScheduler daily job
├── static/                # CSS + JS
├── templates/             # Jinja2 templates
├── run.py                 # entrypoint
└── requirements.txt
```

## Quick start · 快速开始

```bash
git clone <this-repo-url>
cd badminton-topic-pool
python -m pip install -r requirements.txt
python run.py
# open http://127.0.0.1:8000
```

The SQLite database (`data/pool.db`) is created automatically on first run.

## Dynamic weight engine (core) · 动态权重引擎（核心）

- **Goal · 目标**: prioritize pool-entry probability. Uses `log(views)` as the *target variable*, never as a weight feature.
- **Signals · 信号**: finish rate, 2s retention, share rate, favorite rate, comment rate.
- **Algorithm · 算法**: weighted Spearman rank correlation over the full dataset with time decay (half-life defaults to 30 days), absolute values normalized.
- **Auto-recalc · 自动重算**: every create/edit/delete of a work triggers recalculation; falls back to defaults when the sample is < 10; can be locked manually.
- **Audit trail · 留痕**: every recalculation is recorded in the `weight_history` table.
- **Initial weights · 初始权重**: empirically calibrated defaults, defined in `app/config.py` (`DEFAULT_WEIGHTS`).

Weights are **empirical estimates**, not official Douyin coefficients. They are adjustable in Settings.

## Work fields (8 required) · 录入字段（8 项必填）

Script text, publish date, views, 2s skip rate, finish rate, comments, shares, favorites.
Optional: likes, danmu, title, tags.
Direction is auto-extracted by DeepSeek from the configured direction dictionary (can be overridden manually).

2s retention is derived from the skip rate (`ret2s = 1 - jump2`). Works less than 24h old are down-weighted in the weight calculation.

## Candidate generation · 候选生成

DeepSeek direct API (OpenAI-compatible `/chat/completions`; configure API Key / Base URL / Model in Settings). Each candidate contains: direction, title, full narration script (≤ 40 chars), and 3 mandatory tags. When no API key is configured it errors clearly and **never fabricates** scripts. Deduplicated against the last 7 days by similarity.

## Hotspot crawler · 热点爬虫

Primary source: Hupu badminton board. Secondary: Toutiao hot list (with heat values). Fallback: Baidu hot search. Keyword-filtered (羽毛球 / 杀球 / 高远球 / 苏迪曼 …). **When nothing is found, it logs only — never fabricates data.**

## Scheduled push · 定时推送

A local always-on service using APScheduler runs daily at `push_time` (default `20:00`): crawl → compute weights → generate 2 candidates → send email. The full flow can also be triggered manually from the dashboard. Emails are plain-text and concise.

## Configuration & security · 配置与安全

- All configuration lives in the local SQLite DB (`data/pool.db`).
- **API keys and SMTP credentials are stored locally only** and are never echoed back to the page.
- Do **not** commit `data/pool.db` — it contains your local secrets.

## Known limitations · 已知限制

- No official API: works are entered manually; hotspots rely on public sources (Hupu usually has content, but may be empty).
- Initial weights are a preliminary empirical estimate; accuracy improves as you record more works.
- The crawler is subject to public-site anti-scraping; daily content is not guaranteed.
