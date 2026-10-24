"""Deterministic pulse renderer.

FORMAT STATUS: HTML <pre> blocks via parse_mode=HTML are the PROVISIONAL
native-readable table format pending the verified historical publisher
contract; do not treat as the approved format until that contract lands. No user data lives in this
repository: fixed text arrives decrypted at runtime, all numbers arrive
from collectors.
"""
import datetime as dt
from zoneinfo import ZoneInfo

CAIRO = ZoneInfo("Africa/Cairo")
MAX_CHARS = 3800  # Telegram message budget with headroom under 4096


def _stamp(value):
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return parsed


def _table(headers, rows):
    """Fixed-width monospace table for a <pre> block."""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))
    def fmt(row):
        return "  ".join(str(cell).ljust(widths[i]) for i, cell in enumerate(row))
    lines = [fmt(headers), "  ".join("-" * w for w in widths)]
    lines += [fmt(row) for row in rows]
    return "\n".join(lines)


STATES = {"pending": "في الانتظار", "in_progress": "قيد العمل", "parked": "متوقف بأمرك"}
STALE_HOURS = 24


def render(counts, work, curated, fixed_text, generated=None):
    _stamp(counts["snapshot_at"])
    for key in ("total", "open", "closed"):
        if type(counts.get(key)) is not int or counts[key] < 0:
            raise ValueError("invalid count")
    if counts["open"] + counts["closed"] != counts["total"]:
        raise ValueError("count mismatch")
    if not isinstance(work.get("rows"), list) or not work["rows"]:
        raise ValueError("missing work table")
    for row in work["rows"]:
        if type(row.get("open")) is not int or row["open"] < 0 or type(row.get("closed_24h")) is not int or row["closed_24h"] < 0 or not isinstance(row.get("repo"), str):
            raise ValueError("invalid work row")
    if not isinstance(fixed_text, str) or not fixed_text.strip():
        raise ValueError("fixed text unavailable")
    _stamp(curated["snapshot_at"])
    if not isinstance(curated.get("rows"), list) or not curated["rows"]:
        raise ValueError("missing curated rows")
    seen = set()
    for row in curated["rows"]:
        if not isinstance(row.get("label"), str) or not row["label"].strip() or "|" in row["label"] or "\n" in row["label"]:
            raise ValueError("invalid curated label")
        if row["label"] in seen:
            raise ValueError("duplicate curated label")
        seen.add(row["label"])
        if row.get("status") not in STATES:
            raise ValueError("invalid curated status")
    generated = generated or dt.datetime.now(CAIRO)
    ratio = "{:.1f}%".format(100 * counts["closed"] / counts["total"]) if counts["total"] else "غير متاحة"
    read_at = _stamp(counts["snapshot_at"]).astimezone(CAIRO).strftime("%Y-%m-%d %H:%M:%S")
    parts = [
        "<b>النبضة</b>",
        "",
        "وقت قراءة الأرقام: " + read_at + " القاهرة.",
        "أُعدّت النسخة: " + generated.astimezone(CAIRO).strftime("%Y-%m-%d %H:%M:%S") + " القاهرة.",
        "",
        "<b>إشيوز GitHub</b>",
        "",
        "<pre>" + _table(
            ["الإجمالي", "المفتوح", "المغلق", "نسبة المغلق"],
            [[str(counts["total"]), str(counts["open"]), str(counts["closed"]), ratio]],
        ) + "</pre>",
        "",
        "<b>بنود مختارة من أعمالك</b> - حالات الأعمال: " + _stamp(curated["snapshot_at"]).astimezone(CAIRO).strftime("%Y-%m-%d %H:%M") + " القاهرة. ليست القائمة الكاملة، والحالة المسجّلة ليست إثباتًا للنشاط أو الإنجاز.",
        "",
        "<pre>" + _table(
            ["العمل", "الحالة المسجّلة"],
            [[row["label"], STATES[row["status"]]] for row in curated["rows"]],
        ) + "</pre>",
        "",
    ]
    snapshot_age = generated.astimezone(CAIRO) - _stamp(curated["snapshot_at"]).astimezone(CAIRO)
    if snapshot_age > dt.timedelta(hours=STALE_HOURS):
        parts += ["تنبيه: بنود الأعمال لم تُحدَّث منذ أكثر من " + str(STALE_HOURS) + " ساعة (آخر تحديث ظاهر أعلاه).", ""]
    parts += [
        "البنود غير الظاهرة لم تُوصف كمكتملة. لا نسب تقدّم موثقة هنا.",
        "",
        "<b>إشيوز مفتوحة حسب المستودع</b> (وما أُغلق خلال ٢٤ ساعة - نطاق GitHub مستقل لا يُطرح من البنود أعلاه)",
        "",
        "<pre>" + _table(
            ["المستودع", "مفتوح", "أُغلق 24س"],
            [[row["repo"], str(row["open"]), str(row["closed_24h"])] for row in work["rows"]],
        ) + "</pre>",
        "",
        "الأرقام لقطة لحظة القراءة وليست إثبات نشاط أو اكتمال.",
        "",
        "<b>تعليماتك الدائمة</b>",
        "",
        fixed_text.strip(),
        "",
        "إعداد هذه النسخة لا يثبت نشرها أو وصولها.",
    ]
    return "\n".join(parts) + "\n"


def chunk(body, limit=MAX_CHARS):
    """Split on line boundaries; never mid-line and never inside a <pre> block
    if avoidable. Fail closed on an over-long line."""
    if len(body) <= limit:
        return [body]
    parts, current, in_pre = [], "", False
    for line in body.split("\n"):
        if len(line) > limit:
            raise ValueError("single line exceeds send budget")
        if "<pre>" in line:
            in_pre = True
        closing = "</pre>" in line
        if len(current) + len(line) + 1 > limit and not in_pre:
            parts.append(current.rstrip("\n"))
            current = ""
        current += line + "\n"
        if closing:
            in_pre = False
    if current.strip():
        parts.append(current.rstrip("\n"))
    if not parts:
        raise ValueError("empty pulse")
    if any(len(part) > limit for part in parts):
        raise ValueError("unsplittable block exceeds send budget")
    return parts
