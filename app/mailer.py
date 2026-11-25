"""SMTP 邮件推送：纯文本简洁版，两条候选各一段，含四要素。"""

from __future__ import annotations

import json
import logging
import smtplib
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

from . import db

log = logging.getLogger("mailer")


def _need(*keys: str) -> list[str]:
    return [k for k in keys if not db.get_setting(k, "").strip()]


def build_plain_body(candidates: list[dict]) -> str:
    lines = ["今日候选选题（自动生成，仅供参考）", ""]
    for c in candidates:
        tags = c["tags"] if isinstance(c["tags"], list) else json.loads(c["tags"] or "[]")
        lines.extend([
            f"【候选 {c['slot']}】{c['direction']}",
            f"标题：{c['title']}",
            f"文案：{c['script']}",
            f"必带标签：{' '.join('#' + t for t in tags)}",
            "",
        ])
    lines.append("---")
    lines.append("登录网页后台可标记采用、录入发布后数据，引擎会自动重算权重。")
    return "\n".join(lines)


def send_daily(candidates: list[dict]) -> tuple[bool, str]:
    missing = _need("smtp_host", "smtp_user", "smtp_password", "mail_from", "mail_to")
    if missing:
        return False, f"邮件未发送：SMTP 配置不完整，缺少 {', '.join(missing)}。"

    host = db.get_setting("smtp_host").strip()
    port = int(db.get_setting("smtp_port", "465") or 465)
    user = db.get_setting("smtp_user").strip()
    password = db.get_setting("smtp_password").strip()
    mail_from = db.get_setting("mail_from").strip()
    mail_to = db.get_setting("mail_to").strip()
    use_ssl = db.get_setting("smtp_use_ssl", "1") not in ("0", "false", "False")

    body = build_plain_body(candidates)
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(f"羽毛球选题候选 {db.today_str()}", "utf-8")
    msg["From"] = formataddr((str(Header("选题引擎", "utf-8")), mail_from))
    msg["To"] = mail_to

    try:
        if use_ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=20)
        else:
            server = smtplib.SMTP(host, port, timeout=20)
            server.starttls()
        with server:
            server.login(user, password)
            server.sendmail(mail_from, [mail_to], msg.as_string())
        return True, "邮件已发送。"
    except Exception as exc:  # noqa: BLE001
        log.warning("邮件发送失败: %s", exc)
        return False, f"邮件发送失败：{exc}"
