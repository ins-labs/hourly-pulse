# hourly-pulse

A fully deterministic, LLM-free hourly pulse: on the hour, GitHub Actions
collects public-API issue counts, renders a fixed-format Arabic summary with
the owner's approved standing text, and sends it through a Telegram bot to a
configured topic. No data, names, or private content live in this repository;
all bindings are secrets.

## How it works

1. `src/collector.py` reads org issue counts (total/open/closed) via the
   GitHub search API. Partial, malformed, or mutually inconsistent responses
   fail the run; no cached numbers are ever reused.
2. `src/crypto_util.py` decrypts `content/standing_text.enc` at runtime with
   `PULSE_TEXT_KEY`. Failure fails the run.
3. `src/renderer.py` renders the pulse and splits it on line boundaries under
   the Telegram message budget.
4. `src/sender.py` posts each chunk with `message_thread_id` and verifies the
   API receipt (ok, message id, echoed text). Any failure fails the run.

## Secrets (all required)

| Secret | Purpose |
| --- | --- |
| `PULSE_GH_READ_TOKEN` | Read-only token for issue counts |
| `PULSE_GH_ORG` | Organization login whose issues are counted |
| `PULSE_TG_BOT_TOKEN` | Telegram bot token |
| `PULSE_TG_CHAT_ID` | Destination chat id |
| `PULSE_TG_TOPIC_ID` | Destination topic (message_thread_id) |
| `PULSE_TEXT_KEY` | Fernet key for `content/standing_text.enc` |

## Operations

- Schedule: hourly cron; GitHub-hosted scheduling can run late under load.
- Tests run in CI before every send: `python3 -m unittest discover -s tests -v`.
- To change the fixed text, re-encrypt and commit a new `standing_text.enc`
  (see `content/README.md`). Rotate `PULSE_TEXT_KEY` the same way.
- A failed run is visible in the Actions tab; the next scheduled run retries
  naturally. No run retries itself.
