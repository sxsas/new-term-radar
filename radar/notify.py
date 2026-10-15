"""Push a report (Markdown) out, through Resend mail and/or a chat webhook. Optional.

Mail: RESEND_API_KEY, EMAIL_FROM (an address on a domain you verified in Resend),
RADAR_EMAIL_TO (comma-separated for several).

Webhook: RADAR_WEBHOOK_URL, and RADAR_WEBHOOK_FORMAT = slack (default), feishu or
dingtalk. Works with anything else that takes {"text": "..."} too.

Neither channel configured says so and does nothing: a missing recipient must not fail
the run that produced the report. Needs the `markdown` package (pip install markdown).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

from . import UA

# Inline styles: Gmail drops a <style> block that sits outside <head>, which leaves
# tables unbordered with centred headers. Every tag carries its own.
FONT = "-apple-system,'Segoe UI','PingFang SC','Microsoft YaHei',sans-serif"
INLINE = {
    "h2": "font-size:18px;margin:0 0 12px;color:#1f2328",
    "h3": "font-size:15px;margin:24px 0 8px;color:#1f2328",
    "p": "margin:8px 0",
    "table": "border-collapse:collapse;margin:8px 0;width:100%",
    "th": "border:1px solid #d0d7de;padding:6px 10px;font-size:13px;background:#f6f8fa;text-align:left;white-space:nowrap",
    "td": "border:1px solid #d0d7de;padding:6px 10px;font-size:13px;vertical-align:top",
    "ul": "padding-left:0;margin:8px 0;list-style:none",
    "li": "margin:0 0 10px;padding:8px 12px;border-left:3px solid #d0d7de;background:#f6f8fa;font-size:13px",
    "a": "color:#0969da",
}
TAG = re.compile(r"<(" + "|".join(INLINE) + r")(\s[^>]*)?>")
_ADDRESS = re.compile(r"^[^@\s<>\"']+@[^@\s<>\"']+\.[a-z]{2,}$", re.I)

WEBHOOK_MAX = 4000   # reports run long; the table at the top is the part worth reading
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_MD_EMPHASIS = re.compile(r"\*\*([^*]+)\*\*|`([^`]+)`")
_MD_TABLE_SEP = re.compile(r"^[ \t]*\|[\s:|-]+\|[ \t]*$", re.M)
_MD_TABLE_ROW = re.compile(r"^[ \t]*\|(.+)\|[ \t]*$", re.M)


def plain(md_text: str) -> str:
    """Markdown as the chat apps want it: links stay links, tables turn into lines."""
    text = _MD_TABLE_SEP.sub("", md_text)
    text = _MD_TABLE_ROW.sub(lambda m: " · ".join(c.strip() for c in m.group(1).split("|") if c.strip()), text)
    text = _MD_LINK.sub(r"\1 \2", text)
    text = _MD_EMPHASIS.sub(lambda m: m.group(1) or m.group(2), text)
    text = re.sub(r"^[ \t]*[-*][ \t]+", "", text, flags=re.M)
    text = re.sub(r"^#+[ \t]*", "", text, flags=re.M)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) <= WEBHOOK_MAX:
        return text
    cut = text[:WEBHOOK_MAX].rsplit("\n", 1)[0]
    return cut + "\n…"


def webhook_body(fmt: str, subject: str, md_text: str) -> dict:
    text = f"{subject}\n\n{plain(md_text)}" if subject else plain(md_text)
    if fmt == "feishu":
        return {"msg_type": "text", "content": {"text": text}}
    if fmt == "dingtalk":
        return {"msgtype": "text", "text": {"content": text}}
    return {"text": text}      # slack and everything shaped like it


def webhook(subject: str, md_text: str) -> str:
    url = os.environ.get("RADAR_WEBHOOK_URL", "").strip()
    if not url:
        return "webhook skipped: RADAR_WEBHOOK_URL not set"
    fmt = os.environ.get("RADAR_WEBHOOK_FORMAT", "slack").strip().lower() or "slack"
    body = json.dumps(webhook_body(fmt, subject, md_text)).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return f"webhook sent ({res.status})"
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Webhook {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc


def _inline(m: re.Match) -> str:
    attrs = m.group(2) or ""
    if m.group(1) == "td" and 'style="text-align' in attrs:   # markdown's own column alignment
        return f"<td{attrs}>"
    return f'<{m.group(1)} style="{INLINE[m.group(1)]}"{attrs}>'


def html(md_text: str) -> str:
    import markdown
    body = TAG.sub(_inline, markdown.markdown(md_text, extensions=["tables"]))
    return f'<div style="font:14px/1.6 {FONT};color:#1f2328;max-width:900px">{body}</div>'


def recipients(raw: str) -> list[str]:
    """RADAR_EMAIL_TO as people type it: commas (also full-width), semicolons or spaces
    between addresses; quotes and angle brackets around them are dropped."""
    parts = re.split(r"[,，;；\s]+", raw.strip().strip("\"'"))
    return [p.strip("\"'<>") for p in parts if p.strip("\"'<>")]


def send(subject: str, md_text: str) -> str:
    """Every configured channel gets the report; the first refusal ends the run."""
    if not os.environ.get("RADAR_WEBHOOK_URL", "").strip():
        return email(subject, md_text)
    return webhook(subject, md_text) + " / " + email(subject, md_text)


def email(subject: str, md_text: str) -> str:
    to = recipients(os.environ.get("RADAR_EMAIL_TO", ""))
    if not to:
        return "mail skipped: RADAR_EMAIL_TO not set"
    bad = [i + 1 for i, a in enumerate(to) if not _ADDRESS.match(a)]
    if bad:   # the address itself stays out of the log
        raise SystemExit(f"RADAR_EMAIL_TO: {len(to)} address(es), number {bad} is not an email address")
    body = json.dumps({"from": os.environ["EMAIL_FROM"], "to": to, "subject": subject, "html": html(md_text)}).encode()
    # Resend sits behind Cloudflare, which answers Python's default User-Agent with 403 "error code: 1010".
    req = urllib.request.Request("https://api.resend.com/emails", data=body, method="POST", headers={
        "Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return f"mail sent ({res.status})"
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Resend {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
