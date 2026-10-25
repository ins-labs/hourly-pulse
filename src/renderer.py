"""Deterministic pulse renderer: emits explicit Rich Message blocks.

FORMAT: native Telegram Rich Messages (Bot API 10.1+, official contract).
Earlier HTML <pre> rendering was retired after real-client pixel inspection
showed clipping/Arabic-shaping distortion; an earlier claim that the Bot API
had no table capability was stale - the Rich Messages surface has native
bordered tables. Explicit blocks are used (not markdown/html) so no parser
sits between the approved text and Telegram: user text goes in verbatim.

LAYOUT (owner-approved example, 2026-10-25 revision): one dynamic timestamp
heading, the org heading, the bordered windowed metrics table (org lifetime,
7 days, 24 hours, 3 hours - one row per creation window; total, open, closed,
and closed-to-total ratio columns), then the standing
text verbatim - including its own section titles, which therefore live only
inside the encrypted payload and never as literals in this public repo -
then a fixed footer. No coverage-disclaimer paragraph: the owner dropped it.
"""
import datetime as dt
import re
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


def _ratio(total, closed):
    return "{:.1f}%".format(100 * closed / total) if total else "غير متاحة"


def render_blocks(counts, fixed_text, generated=None):
    """Build the full rich-message block list. Fail closed on any invalid input."""
    _stamp(counts["snapshot_at"])
    _stamp(counts["org_created_at"])
    if not isinstance(counts.get("org"), str) or not counts["org"].strip() or any(c in counts["org"] for c in "<>&"):
        raise ValueError("invalid org name")
    rows = counts.get("rows")
    if not isinstance(rows, list) or len(rows) != 4:
        raise ValueError("invalid window rows")
    for row in rows:
        if not isinstance(row.get("label"), str) or not row["label"].strip():
            raise ValueError("invalid window label")
        for key in ("opened", "still_open", "closed"):
            if type(row.get(key)) is not int or row[key] < 0:
                raise ValueError("invalid window count")
        if row["still_open"] > row["opened"]:
            raise ValueError("still-open exceeds opened")
    generated = generated or dt.datetime.now(CAIRO)
    stamp = generated.astimezone(CAIRO).strftime("%Y-%m-%d %H:%M:%S")

    cells = [[
        _cell("المدة", True),
        _cell("الإجمالي", True),
        _cell("المفتوح", True),
        _cell("تم الغلق", True),
        _cell("نسبة المفتوح (الى اجمالي الفترة)", True),
        _cell("نسبة الاغلاق (إلى إجمالي الفترة)", True),
    ]]
    for row in counts["rows"]:
        # owner definitions, final 04:15:14: الإجمالي = created in the window
        # whatever their state; المفتوح = created in the window and STILL open;
        # تم الغلق = distinct issues with a close event in the window; ratios
        # to the window total, closed ratio may exceed 100%.
        cells.append([
            _cell(row["label"]),
            _cell(str(row["opened"])),
            _cell(str(row["still_open"])),
            _cell(str(row["closed"])),
            _cell(_ratio(row["opened"], row["still_open"])),
            _cell(_ratio(row["opened"], row["closed"])),
        ])

    blocks = [
        _heading("نبضة " + stamp + " القاهرة", 2),
        _heading("مهام " + counts["org"] + " على GitHub", 3),
        {"type": "table", "is_bordered": True, "cells": cells},
    ]
    # Fixed text verbatim: split on blank-line runs into paragraph blocks
    # (a run of 2+ newlines, possibly with whitespace, is one separator), so no
    # paragraph ever starts or ends with separator newlines. Wording untouched.
    for chunk in re.split(r"\n[ \t]*\n(?:[ \t]*\n)*", fixed_text.strip()):
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
