"""Telegram Bot API rich-message sender (sendRichMessage) with strong
structural readback verification.

The receipt's returned rich_message is verified against the intended blocks:
ok flag, integer message id, destination chat id, destination topic
(message_thread_id), block count and types, the FULL plain text of every
block, and the table's exact cell texts plus header/align/border flags.
Fails closed on any mismatch; a run never retries itself.
"""
import json
import urllib.request

API = "https://api.telegram.org"


def _plain(rich):
    """Extract full plain text from a RichText value (string, list, or typed
    inline object). Unknown structures fail closed."""
    if isinstance(rich, str):
        return rich
    if isinstance(rich, list):
        return "".join(_plain(item) for item in rich)
    if isinstance(rich, dict):
        if "text" in rich:
            return _plain(rich["text"])
        raise ValueError("unverifiable rich text element: " + str(rich.get("type")))
    raise ValueError("unverifiable rich text value")


def _block_text(block):
    if not isinstance(block, dict):
        raise ValueError("malformed receipt block")
    kind = block.get("type")
    if kind in ("paragraph", "heading", "footer", "preformatted"):
        return _plain(block.get("text", ""))
    if kind == "table":
        rows = block.get("cells")
        if not isinstance(rows, list):
            raise ValueError("malformed receipt table")
        return "\n".join("\x1f".join(_plain(cell.get("text", "")) for cell in row) for row in rows)
    raise ValueError("unexpected receipt block type: " + str(kind))


def expected_block_texts(blocks):
    return [_block_text(block) for block in blocks]


def send_rich(token, chat_id, topic_id, blocks, opener=urllib.request.urlopen):
    if not token or not chat_id or not topic_id:
        raise ValueError("sender binding incomplete")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("empty rich message")
    payload = json.dumps({
        "chat_id": int(chat_id),
        "message_thread_id": int(topic_id),
        "rich_message": {"blocks": blocks, "is_rtl": True, "skip_entity_detection": True},
    }).encode()
    request = urllib.request.Request(
        API + "/bot" + token + "/sendRichMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with opener(request, timeout=30) as response:
        item = json.load(response)
    if item.get("ok") is not True:
        raise ValueError("rich send not acknowledged")
    result = item.get("result") or {}
    message_id = result.get("message_id")
    if type(message_id) is not int:
        raise ValueError("receipt missing message id")
    if str((result.get("chat") or {}).get("id")) != str(chat_id):
        raise ValueError("receipt chat mismatch")
    if str(result.get("message_thread_id")) != str(topic_id):
        raise ValueError("receipt topic mismatch")
    got_blocks = (result.get("rich_message") or {}).get("blocks")
    if not isinstance(got_blocks, list):
        raise ValueError("receipt missing rich blocks")
    if len(got_blocks) != len(blocks):
        raise ValueError("receipt block-count mismatch")
    want_texts = expected_block_texts(blocks)
    for i, (want_block, got_block) in enumerate(zip(blocks, got_blocks)):
        if got_block.get("type") != want_block["type"]:
            raise ValueError("receipt block " + str(i) + " type mismatch")
        if _block_text(got_block) != want_texts[i]:
            raise ValueError("receipt block " + str(i) + " text mismatch")
        if want_block["type"] == "table":
            got_cells = got_block.get("cells") or []
            for want_row, got_row in zip(want_block["cells"], got_cells):
                for want_cell, got_cell in zip(want_row, got_row):
                    if bool(got_cell.get("is_header")) != bool(want_cell.get("is_header")):
                        raise ValueError("receipt table header-flag mismatch")
                    if got_cell.get("align") != want_cell["align"] or got_cell.get("valign") != want_cell["valign"]:
                        raise ValueError("receipt table cell alignment mismatch")
            if bool(got_block.get("is_bordered")) != bool(want_block.get("is_bordered")):
                raise ValueError("receipt table border mismatch")
    return message_id
