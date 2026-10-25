"""Telegram bot sender (HTML parse_mode) with strong readback verification.

Every chunk's receipt is verified against: ok flag, integer message id,
destination chat id, destination topic (message_thread_id), the FULL expected
plain text (Telegram strips HTML tags in result.text, so the expected value
is exactly computable), and formatting entities matching the tags sent.
Fails closed on any mismatch; a run never retries itself.
"""
import json
import re
import urllib.parse
import urllib.request

API = "https://api.telegram.org"
TAG = re.compile(r"<(/?)(b|pre)>")


def expected_plain(text):
    return TAG.sub("", text)


def expected_entities(text):
    """Count of formatting entities the sent markup must produce."""
    return {"bold": text.count("<b>"), "pre": text.count("<pre>")}


def normalize(text):
    """Canonical wire form: no trailing spaces on any line, no leading/trailing
    blank space on the whole message. Telegram trims these in result.text; we
    never send them, so sent == expected == returned under that benign
    normalization."""
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


def _diff_index(want, got):
    for i, (a, b) in enumerate(zip(want, got)):
        if a != b:
            return i
    return min(len(want), len(got))


def send_chunk(token, chat_id, topic_id, text, opener=urllib.request.urlopen):
    text = normalize(text)
    if not expected_plain(text).strip():
        raise ValueError("empty chunk")
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "message_thread_id": topic_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode()
    request = urllib.request.Request(
        API + "/bot" + token + "/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with opener(request, timeout=30) as response:
        item = json.load(response)
    if item.get("ok") is not True:
        raise ValueError("send not acknowledged")
    result = item.get("result") or {}
    message_id = result.get("message_id")
    if type(message_id) is not int:
        raise ValueError("receipt missing message id")
    chat = result.get("chat") or {}
    if str(chat.get("id")) != str(chat_id):
        raise ValueError("receipt chat mismatch")
    if str(result.get("message_thread_id")) != str(topic_id):
        raise ValueError("receipt topic mismatch")
    expected = expected_plain(text)
    got_text = result.get("text")
    if got_text != expected:
        at = _diff_index(expected, got_text if isinstance(got_text, str) else "")
        raise ValueError(
            "receipt plain-text mismatch at char " + str(at)
            + " (expected len " + str(len(expected)) + ", got len " + str(len(got_text or "")) + ")"
        )
    want = expected_entities(text)
    got = {"bold": 0, "pre": 0}
    for entity in result.get("entities") or []:
        kind = (entity or {}).get("type")
        if kind in got:
            got[kind] += 1
    if got != want:
        raise ValueError("receipt formatting-entity mismatch")
    return message_id


def send_pulse(token, chat_id, topic_id, parts, opener=urllib.request.urlopen):
    if not token or not chat_id or not topic_id:
        raise ValueError("sender binding incomplete")
    receipts = []
    for part in parts:
        receipts.append(send_chunk(token, chat_id, topic_id, part, opener))
    return receipts
