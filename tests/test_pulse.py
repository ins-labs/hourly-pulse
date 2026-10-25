"""Synthetic, network-free unit tests for the hourly pulse.

Every transport is a fake opener; no secret, token, or real endpoint is
touched. Fail-closed behavior is tested as thoroughly as the happy path.
"""
import datetime as dt
import io
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collector
import crypto_util
import renderer
import sender

NOW = dt.datetime.fromisoformat("2026-10-25T02:07:00+03:00")
ORG_CREATED = "2024-05-01T10:00:00+00:00"
FIXED = "سطر ثابت أول\nسطر ثابت ثان"


class Response(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def opener_of(bodies):
    stream = iter(bodies)
    return lambda *a, **k: Response(json.dumps(next(stream)))


def counts(rows=None):
    if rows is None:
        rows = [(2147, 584, 1567), (100, 40, 60), (12, 5, 7), (0, 0, 3)]
    # one label per row - zip against a fixed 4-label list would silently
    # truncate a longer fixture and hide the invalid row count (CI catch)
    base = ["منذ البداية", "آخر ٧ أيام", "آخر ٢٤ ساعة", "آخر ٣ ساعات"]
    labels = [base[i] if i < len(base) else "row " + str(i) for i in range(len(rows))]
    return {
        "rows": [{"label": l, "opened": a, "still_open": b, "closed": c} for l, (a, b, c) in zip(labels, rows)],
        "org": "some-org",
        "org_created_at": ORG_CREATED,
        "snapshot_at": NOW.isoformat(),
    }


def count_body(n):
    return {"total_count": n, "incomplete_results": False, "items": []}


def ev_closed(issue_id, ts, pr=False):
    issue = {"id": issue_id}
    if pr:
        issue["pull_request"] = {"url": "x"}
    return {"event": "closed", "created_at": ts, "issue": issue}


def ev_other(name, issue_id, ts):
    return {"event": name, "created_at": ts, "issue": {"id": issue_id}}


def make_opener(repos, events_by_repo, count_bodies, org_body=None):
    """Fake transport routed on the URL path, query-insensitive.

    Mirrors the exact endpoints the collector calls:
      GET /orgs/{org}/repos?per_page=100&page=N          -> repos (page 1; then [])
      GET /repos/{owner}/{repo}/issues/events?...        -> events_by_repo[repo][page-1]
      GET /search/issues?q=...&per_page=1                -> next count body, in call order
      GET /orgs/{org}                                    -> org body
    Unknown paths fail the test loudly instead of falling through.
    """
    import urllib.parse as up
    counts = iter(count_bodies)
    seen = []

    def opener(req, timeout=30):
        url = req.full_url
        seen.append(url)
        parsed = up.urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        page = int(up.parse_qs(parsed.query).get("page", ["1"])[0])
        if parts == ["search", "issues"]:
            return Response(json.dumps(next(counts)))
        if len(parts) == 5 and parts[0] == "repos" and parts[3] == "issues" and parts[4] == "events":
            repo = up.unquote(parts[2])
            pages = events_by_repo.get(repo, [[]])
            return Response(json.dumps(pages[page - 1] if page <= len(pages) else []))
        if len(parts) == 3 and parts[0] == "orgs" and parts[2] == "repos":
            return Response(json.dumps(repos if page == 1 else []))
        if len(parts) == 2 and parts[0] == "orgs":
            return Response(json.dumps(org_body if org_body is not None else {"created_at": ORG_CREATED}))
        raise AssertionError("fake opener received unexpected URL: " + url)

    opener.seen = seen
    return opener


class CollectorTests(unittest.TestCase):
    def test_counts_happy(self):
        events = {
            "r1": [[
                ev_closed(10, "2026-10-20T00:00:00Z"),            # in 7d, not 24h
                ev_closed(11, "2026-10-01T00:00:00Z"),            # lifetime only
                ev_closed(12, "2026-10-24T22:00:00Z", pr=True),   # PR: excluded
                ev_other("labeled", 13, "2026-10-24T22:00:00Z"),  # other type: excluded
                # reopened alone NEVER counts: its actual closed event is
                # outside the window, so issue 30 must not appear anywhere
                ev_other("reopened", 30, "2026-10-24T23:00:00Z"),
            ]],
            "r2": [[
                ev_closed(20, "2026-10-24T22:00:00Z"),            # 22:00Z is within 3h of the 23:07Z snapshot: in ALL windows
                ev_closed(21, "2026-10-24T23:05:00Z"),            # in 3h too
                ev_closed(21, "2026-10-24T23:06:00Z"),            # second cycle: still one issue
                ev_closed(22, "2026-10-24T23:09:00Z"),            # after snapshot: excluded
            ]],
        }
        count_bodies = [count_body(10), count_body(4)] + [count_body(5), count_body(2)] + [count_body(3), count_body(1)] + [count_body(1), count_body(0)]
        opener = make_opener([{"name": "r1"}, {"name": "r2"}], events, count_bodies)
        out = collector.collect_counts("t", "some-org", opener=opener, now=NOW)
        self.assertEqual(out["org"], "some-org")
        self.assertTrue(out["org_created_at"].startswith("2024-05-01"))
        self.assertEqual([r["label"] for r in out["rows"]], ["منذ البداية", "آخر ٧ أيام", "آخر ٢٤ ساعة", "آخر ٣ ساعات"])
        # boundaries verified by datetime computation against the 2026-10-24T23:07:00Z
        # snapshot: 3h start is 20:07Z, so issue 20 (22:00Z) and 21 (23:05Z) both count
        self.assertEqual([(r["opened"], r["still_open"], r["closed"]) for r in out["rows"]],
                         [(10, 4, 4), (5, 2, 3), (3, 1, 2), (1, 0, 2)])

    def test_window_qualifier(self):
        opener = make_opener([{"name": "r1"}], {"r1": [[]]}, [count_body(0)] * 8)
        out = collector.collect_counts("t", "some-org", opener=opener, now=NOW)
        import urllib.parse
        search_urls = [u for u in opener.seen if "/search/issues" in u]
        self.assertEqual(len(search_urls), 8)
        queries = [urllib.parse.parse_qs(urllib.parse.urlparse(u).query)["q"][0] for u in search_urls]
        self.assertEqual(queries[0], "org:some-org is:issue")
        self.assertEqual(queries[1], "org:some-org is:issue is:open")
        self.assertEqual(queries[2], "org:some-org is:issue created:>=2026-10-18T02:07:00+03:00")
        self.assertEqual(queries[3], "org:some-org is:issue created:>=2026-10-18T02:07:00+03:00 is:open")
        self.assertEqual(queries[4], "org:some-org is:issue created:>=2026-10-24T02:07:00+03:00")
        self.assertEqual(queries[6], "org:some-org is:issue created:>=2026-10-24T23:07:00+03:00")
        self.assertTrue(any("/repos/some-org/r1/issues/events" in u for u in opener.seen))

    def test_events_pagination(self):
        page1 = [ev_other("labeled", 1000 + i, "2026-10-24T00:00:00Z") for i in range(100)]
        page2 = [ev_closed(7, "2026-10-24T01:00:00Z")]
        opener = make_opener([{"name": "r1"}], {"r1": [page1, page2]}, [count_body(0)] * 8)
        out = collector.collect_counts("t", "some-org", opener=opener, now=NOW)
        self.assertEqual(out["rows"][0]["closed"], 1)
        pages = [u for u in opener.seen if "issues/events" in u]
        self.assertEqual(len(pages), 2)

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            collector.collect_counts("", "org")
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "")
        # naive snapshot
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener([], {}, []), now=dt.datetime(2026, 10, 25, 2, 7))
        # repos response not a list
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener({"oops": 1}, {}, []))
        # events response not a list
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener([{"name": "r1"}], {"r1": [{"oops": 1}]}, []))
        # close event missing issue id
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener([{"name": "r1"}], {"r1": [[{"event": "closed", "created_at": "2026-10-24T00:00:00Z", "issue": {}}]]}, []))
        # event page budget exceeded
        saved = collector.MAX_EVENT_PAGES
        collector.MAX_EVENT_PAGES = 1
        try:
            with self.assertRaises(ValueError):
                collector.collect_counts("t", "o", opener=make_opener([{"name": "r1"}], {"r1": [[ev_other("labeled", i, "2026-10-24T00:00:00Z") for i in range(100)]]}, []))
        finally:
            collector.MAX_EVENT_PAGES = saved
        # search malformed / incomplete / still-open above opened / window above lifetime
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener([], {}, [{"items": []}]))
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener([], {}, [{"total_count": 1, "incomplete_results": True}]))
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener([], {}, [count_body(2), count_body(3)]))
        # window total above lifetime: the collector fetches ALL eight counts
        # before the post-loop invariant check, so the queue needs all eight
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener([], {}, [count_body(2), count_body(1)] + [count_body(9), count_body(1)] + [count_body(0)] * 4))
        # org response without created_at fails closed after all searches
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=make_opener([], {}, [count_body(0)] * 8, org_body={"login": "o"}))


class RendererTests(unittest.TestCase):
    def test_blocks_structure(self):
        blocks = renderer.render_blocks(counts(), FIXED, generated=NOW)
        types = [b["type"] for b in blocks]
        self.assertEqual(types, ["heading", "heading", "table", "paragraph", "paragraph"])
        self.assertEqual(blocks[0]["text"], "نبضة 2026-10-25 02:07:00 القاهرة")
        self.assertEqual(blocks[0]["size"], 2)
        self.assertEqual(blocks[1]["text"], "مهام some-org على GitHub")
        self.assertEqual(blocks[1]["size"], 3)
        table = blocks[2]
        self.assertTrue(table["is_bordered"])
        self.assertEqual(len(table["cells"]), 5)
        headers = table["cells"][0]
        self.assertEqual([c["text"] for c in headers], ["الفترة", "الإجمالي", "المفتوح", "نسبة المفتوح", "المغلق", "نسبة الغلق"])
        for c in headers:
            self.assertTrue(c["is_header"])
            self.assertEqual(c["align"], "center")
            self.assertEqual(c["valign"], "middle")
        self.assertEqual([r[0]["text"] for r in table["cells"][1:]],
                         ["منذ البداية", "آخر ٧ أيام", "آخر ٢٤ ساعة", "آخر ٣ ساعات"])
        # owner column order 04:44:07: metric then its ratio, per pair
        self.assertEqual([c["text"] for c in table["cells"][1]], ["منذ البداية", "2147", "584", "27.2%", "1567", "73.0%"])
        self.assertEqual([c["text"] for c in table["cells"][2]], ["آخر ٧ أيام", "100", "40", "40.0%", "60", "60.0%"])
        self.assertEqual([c["text"] for c in table["cells"][3]], ["آخر ٢٤ ساعة", "12", "5", "41.7%", "7", "58.3%"])
        # zero-total window: truthful غير متاحة, never 0.0% or division by zero;
        # closed may exceed opened (backlog closing) without failing
        self.assertEqual([c["text"] for c in table["cells"][4]], ["آخر ٣ ساعات", "0", "0", "غير متاحة", "3", "غير متاحة"])
        for r in table["cells"][1:]:
            for c in r:
                self.assertNotIn("is_header", c)
                self.assertEqual(c["align"], "center")
                self.assertEqual(c["valign"], "middle")
        self.assertEqual(blocks[3]["text"], "سطر ثابت أول\nسطر ثابت ثان")
        self.assertEqual(blocks[4]["text"], "إعداد هذه النسخة لا يثبت نشرها أو وصولها.")

    def test_verbatim_paragraph_split(self):
        text = "فقرة أولى\nسطرها الثاني\n\nفقرة ثانية\n\n\nفقرة ثالثة"
        blocks = renderer.render_blocks(counts(), text, generated=NOW)
        # exact paragraph order against the actual renderer contract:
        # fixed text verbatim paragraphs, then the footer (the timestamp and
        # org lines are headings, not paragraphs; no disclaimer paragraph).
        paras = [b["text"] for b in blocks if b["type"] == "paragraph"]
        self.assertEqual(len(paras), 4)
        self.assertEqual(paras[:-1], ["فقرة أولى\nسطرها الثاني", "فقرة ثانية", "فقرة ثالثة"])
        self.assertEqual(paras[-1], "إعداد هذه النسخة لا يثبت نشرها أو وصولها.")

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            renderer.render_blocks(counts([(-1, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)]), FIXED)
        with self.assertRaises(ValueError):
            renderer.render_blocks(counts([(5, 6, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)]), FIXED)
        with self.assertRaises(ValueError):
            renderer.render_blocks(counts([(1, 0, 0), (0, 0, 0), (0, 0, 0)]), FIXED)
        with self.assertRaises(ValueError):
            renderer.render_blocks(counts([(1, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0), (0, 0, 0)]), FIXED)
        bad_label = counts()
        bad_label["rows"][0]["label"] = "  "
        with self.assertRaises(ValueError):
            renderer.render_blocks(bad_label, FIXED)
        with self.assertRaises(ValueError):
            renderer.render_blocks(counts(), "   ")
        bad = counts()
        bad["org"] = "x<y"
        with self.assertRaises(ValueError):
            renderer.render_blocks(bad, FIXED)
        huge = counts()
        with self.assertRaises(ValueError):
            renderer.render_blocks(huge, "س\n\n" * 20000)


def rich_receipt(chat_id, topic_id, blocks, mid=111):
    return {
        "ok": True,
        "result": {
            "message_id": mid,
            "chat": {"id": int(chat_id)},
            "message_thread_id": int(topic_id),
            "rich_message": {"blocks": json.loads(json.dumps(blocks)), "is_rtl": True},
        },
    }


class SenderTests(unittest.TestCase):
    def setUp(self):
        self.blocks = renderer.render_blocks(counts(), FIXED, generated=NOW)

    def test_send_happy(self):
        opener = opener_of([rich_receipt("-100123", "43", self.blocks)])
        out = sender.send_rich("tok", "-100123", "43", self.blocks, opener=opener)
        self.assertEqual(out, 111)

    def test_payload_shape(self):
        captured = {}
        def opener(req, timeout=30):
            captured.update(json.loads(req.data.decode()))
            return Response(json.dumps(rich_receipt("-100123", "43", self.blocks)))
        sender.send_rich("tok", "-100123", "43", self.blocks, opener=opener)
        self.assertEqual(captured["chat_id"], -100123)
        self.assertEqual(captured["message_thread_id"], 43)
        rm = captured["rich_message"]
        self.assertTrue(rm["is_rtl"])
        self.assertTrue(rm["skip_entity_detection"])
        self.assertEqual(rm["blocks"], self.blocks)
        self.assertNotIn("text", captured)

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            sender.send_rich("", "-100123", "43", self.blocks)
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", [])
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", self.blocks, opener=opener_of([{"ok": False}]))
        wrong_chat = rich_receipt("-100999", "43", self.blocks)
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", self.blocks, opener=opener_of([wrong_chat]))
        wrong_topic = rich_receipt("-100123", "99", self.blocks)
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", self.blocks, opener=opener_of([wrong_topic]))
        no_blocks = rich_receipt("-100123", "43", self.blocks)
        del no_blocks["result"]["rich_message"]
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", self.blocks, opener=opener_of([no_blocks]))
        tampered = rich_receipt("-100123", "43", self.blocks)
        tampered["result"]["rich_message"]["blocks"][2]["cells"][1][0]["text"] = "9999"
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", self.blocks, opener=opener_of([tampered]))
        bad_align = rich_receipt("-100123", "43", self.blocks)
        bad_align["result"]["rich_message"]["blocks"][2]["cells"][0][0]["align"] = "left"
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", self.blocks, opener=opener_of([bad_align]))
        short = rich_receipt("-100123", "43", self.blocks)
        short["result"]["rich_message"]["blocks"] = short["result"]["rich_message"]["blocks"][:-1]
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", self.blocks, opener=opener_of([short]))


class CryptoTests(unittest.TestCase):
    def test_roundtrip(self):
        key = crypto_util.generate_key()
        blob = crypto_util.encrypt_text(key, FIXED)
        self.assertNotIn("سطر", blob)
        self.assertEqual(crypto_util.decrypt_text(key, blob), FIXED)

    def test_fail_closed(self):
        key = crypto_util.generate_key()
        blob = crypto_util.encrypt_text(key, FIXED)
        with self.assertRaises(ValueError):
            crypto_util.decrypt_text(crypto_util.generate_key(), blob)
        with self.assertRaises(ValueError):
            crypto_util.decrypt_text("", blob)
        with self.assertRaises(ValueError):
            crypto_util.encrypt_text(key, "  ")


if __name__ == "__main__":
    unittest.main()
