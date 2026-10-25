"""Deterministic pulse renderer: emits explicit Rich Message blocks.

FORMAT: native Telegram Rich Messages (Bot API 10.1+, official contract).
Earlier HTML <pre> rendering was retired after real-client pixel inspection
showed clipping/Arabic-shaping distortion; an earlier claim that the Bot API
had no table capability was stale - the Rich Messages surface has native
bordered tables. Explicit blocks are used (not markdown/html) so no parser
sits between the approved text and Telegram: user text goes in verbatim.
"""
import datetime as dt
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
MAX_RICH_CHARS = 32768  # official rich-message text budget
MAX_BLOCKS = 500
MAX_TABLE_COLS = 20


def _stamp(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed


def _cell(text, header=False):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty table cell")
    cell = {"text": text, "align": "center", "valign": "middle"}
    if header:
        cell["is_header"] = True
    return cell


def _para(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError("empty paragraph")
    return {"type": "paragraph", "text": text.strip()}


def _heading(text, size):
    if size not in (1, 2, 3, 4, 5, 6):
        raise ValueError("invalid heading size")
    return {"type": "heading", "text": text, "size": size}


def render_blocks(counts, fixed_text, generated=None):
    """Build the full rich-message block list. Fail closed on any invalid input."""
    _stamp(counts["snapshot_at"])
    _stamp(counts["org_created_at"])
    if not isinstance(counts.get("org"), str) or not counts["org"].strip() or any(c in counts["org"] for c in "<>&"):
        raise ValueError("invalid org name")
    for key in ("total", "open", "closed"):
        if type(counts.get(key)) is not int or counts[key] < 0:
            raise ValueError("invalid count")
    if counts["open"] + counts["closed"] != counts["total"]:
        raise ValueError("count mismatch")
    if not isinstance(fixed_text, str) or not fixed_text.strip():
        raise ValueError("fixed text unavailable")
    generated = generated or dt.datetime.now(CAIRO)
    ratio = "{:.1f}%".format(100 * counts["closed"] / counts["total"]) if counts["total"] else "غير متاحة"
    read_at = _stamp(counts["snapshot_at"]).astimezone(CAIRO).strftime("%Y-%m-%d %H:%M:%S")
    created = _stamp(counts["org_created_at"]).astimezone(CAIRO).strftime("%Y-%m-%d")

    blocks = [
        _heading("النبضة", 2),
        _para("وقت قراءة الأرقام: " + read_at + " القاهرة.\nأُعدّت النسخة: " + generated.astimezone(CAIRO).strftime("%Y-%m-%d %H:%M:%S") + " القاهرة."),
        _heading("مهام " + counts["org"] + " على GitHub", 3),
        {
            "type": "table",
            "is_bordered": True,
            "cells": [
                [_cell("الإجمالي", True), _cell("المفتوح", True), _cell("نسبة المغلق إلى الإجمالي", True)],
                [_cell(str(counts["total"])), _cell(str(counts["open"])), _cell(ratio)],
            ],
        },
        _para("إشيوز GitHub فقط (لا طلبات دمج)، من كل مستودعات المؤسسة المرئية لحساب القراءة، منذ إنشاء " + counts["org"] + " في " + created + " حتى وقت القراءة أعلاه. الأرقام لقطة لحظتها وليست إثبات نشاط أو اكتمال."),
        _heading("تعليماتك الدائمة", 3),
    ]
    # Fixed text verbatim: split only on blank lines into paragraph blocks;
    # concatenation of paragraph texts with blank lines reproduces the input.
    for chunk in fixed_text.strip().split("\n\n"):
        blocks.append(_para(chunk))
    blocks.append(_para("إعداد هذه النسخة لا يثبت نشرها أو وصولها."))

    if len(blocks) > MAX_BLOCKS:
        raise ValueError("block budget exceeded")
    total_chars = 0
    for block in blocks:
        if block["type"] == "table":
            if any(len(row) > MAX_TABLE_COLS for row in block["cells"]):
                raise ValueError("table column budget exceeded")
            total_chars += sum(len(c["text"]) for row in block["cells"] for c in row)
        else:
            total_chars += len(block["text"])
    if total_chars > MAX_RICH_CHARS:
        raise ValueError("rich text budget exceeded")
    return blocks
