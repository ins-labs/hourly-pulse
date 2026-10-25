"""Hourly pulse entrypoint: collect -> render -> send. Fully deterministic,
no LLM, no external orchestrator. Exits non-zero (visible failure) on any
unverified step; one run never retries itself.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import collector
import crypto_util
import renderer
import sender

REPO_ROOT = Path(__file__).resolve().parents[1]
ENC_FILE = REPO_ROOT / "content" / "standing_text.enc"


def main():
    gh_token = os.environ.get("PULSE_GH_READ_TOKEN", "")
    gh_org = os.environ.get("PULSE_GH_ORG", "")
    bot_token = os.environ.get("PULSE_TG_BOT_TOKEN", "")
    chat_id = os.environ.get("PULSE_TG_CHAT_ID", "")
    topic_id = os.environ.get("PULSE_TG_TOPIC_ID", "")
    text_key = os.environ.get("PULSE_TEXT_KEY", "")
    missing = [
        name
        for name, value in [
            ("PULSE_GH_READ_TOKEN", gh_token),
            ("PULSE_GH_ORG", gh_org),
            ("PULSE_TG_BOT_TOKEN", bot_token),
            ("PULSE_TG_CHAT_ID", chat_id),
            ("PULSE_TG_TOPIC_ID", topic_id),
            ("PULSE_TEXT_KEY", text_key),
        ]
        if not value
    ]
    if missing:
        raise SystemExit("missing bindings: " + ", ".join(missing))

    counts = collector.collect_counts(gh_token, gh_org)
    fixed_text = crypto_util.decrypt_text(text_key, ENC_FILE.read_text())
    blocks = renderer.render_blocks(counts, fixed_text)
    message_id = sender.send_rich(bot_token, chat_id, topic_id, blocks)
    print("pulse sent: rich message_id=" + str(message_id) + " blocks=" + str(len(blocks)))


if __name__ == "__main__":
    main()
