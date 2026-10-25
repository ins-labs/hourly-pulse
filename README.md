# hourly-pulse

A fully deterministic, LLM-free hourly pulse: on the hour, GitHub Actions
collects the organization's GitHub issue counts (total / open / closed /
closed-percentage, issues only - never pull requests) over four creation
windows: org lifetime, last 7 days, last 24 hours, last 3 hours, renders a fixed-format
Arabic summary with the owner's approved standing text, and sends it through a
Telegram bot to a configured forum topic. No data, names, or private content
live in this repository; all bindings are secrets.

## How it works

1. `src/collector.py` measures four windows (org lifetime, 7 days, 24 hours,
   3 hours), all anchored to one snapshot: issues created in the window and
   how many of those are still open (search API), and distinct issues with a
   close event in the window regardless of when they were created. Close
   counts come from each repo's full issue-events history - never from the
   search `closed:` qualifier, which indexes only the current closed_at and
   would miss issues closed then reopened (such issues exist in this org). A
   hard page budget fails the run if event volume ever explodes. Partial,
   malformed, or mutually inconsistent responses fail the run; no cached
   numbers are ever reused. Coverage is every repository visible to the read
   account.
2. `src/crypto_util.py` decrypts `content/standing_text.enc` at runtime with
   `PULSE_TEXT_KEY`. Failure fails the run.
3. `src/renderer.py` builds explicit Telegram Rich Message blocks: a
   timestamp heading, the org heading, a native bordered table of the
   window metrics (total / still open / closed / both ratios to the window
   total), the standing text as verbatim paragraphs (split only on blank
   lines; no markup parser touches the approved text, and its section titles
   live only inside the encrypted payload), and a fixed footer. Rich Messages are a Bot API 10.1+ feature with native
   tables; an earlier HTML `<pre>` design was retired after real-client pixel
   inspection showed clipping, and no image rendering is used anywhere.
4. `src/sender.py` posts one `sendRichMessage` call (RTL, entity detection
   off) and verifies the receipt structurally: ok flag, message id, chat id,
   thread id, block count and types, full plain text of every block, and the
   table's exact cells, header flags, alignment, and border. Any mismatch
   fails the run.

## Secrets (all required)

| Secret | Purpose |
| --- | --- |
| `PULSE_GH_READ_TOKEN` | Read-only token for issue counts and org metadata |
| `PULSE_GH_ORG` | Organization login whose issues are counted |
| `PULSE_TG_BOT_TOKEN` | Telegram bot token |
| `PULSE_TG_CHAT_ID` | Destination chat id |
| `PULSE_TG_TOPIC_ID` | Destination topic (message_thread_id) |
| `PULSE_TEXT_KEY` | Fernet key for `content/standing_text.enc` |

## Operations

- Schedule: hourly cron; GitHub-hosted scheduling can run late under load.
- Tests run in CI before every send: `python3 -m unittest discover -s tests -v`.
- Pull requests and branch pushes run `tests` (`.github/workflows/test.yml`)
  with no secrets and no send; the publisher workflow never runs on PRs.
- To change the fixed text, re-encrypt and commit a new `standing_text.enc`
  (see `content/README.md`). Rotate `PULSE_TEXT_KEY` the same way.
- A failed run is visible in the Actions tab; the next scheduled run retries
  naturally. No run retries itself.
