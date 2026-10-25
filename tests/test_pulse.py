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


def counts(t=2147, o=580, c=1567):
    return {
        "total": t, "open": o, "closed": c,
        "org_created_at": ORG_CREATED,
        "snapshot_at": NOW.isoformat(),
    }


def count_body(n):
    return {"total_count": n, "incomplete_results": False, "items": []}


class CollectorTests(unittest.TestCase):
    def test_counts_happy(self):
        opener = opener_of([count_body(10), count_body(4), count_body(6), {"created_at": ORG_CREATED}])
        out = collector.collect_counts("t", "some-org", opener=opener, now=NOW)
        self.assertEqual(out["total"], 10)
        self.assertEqual(out["open"], 4)
        self.assertEqual(out["closed"], 6)
        self.assertTrue(out["org_created_at"].startswith("2024-05-01"))
        self.assertEqual(out["snapshot_at"], NOW.isoformat())

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            collector.collect_counts("", "org")
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "")
        with self.assertRaises(ValueError):  # open+closed != total
            collector.collect_counts("t", "o", opener=opener_of([count_body(10), count_body(4), count_body(4)]))
        with self.assertRaises(ValueError):  # incomplete results
            collector.collect_counts("t", "o", opener=opener_of([{"total_count": 1, "incomplete_results": True}]))
        with self.assertRaises(ValueError):  # malformed count
            collector.collect_counts("t", "o", opener=opener_of([{"total_count": "lots"}]))
        with self.assertRaises(ValueError):  # org response without created_at
            collector.collect_counts("t", "o", opener=opener_of([count_body(2), count_body(1), count_body(1), {"login": "o"}]))


class RendererTests(unittest.TestCase):
    def test_render_markers(self):
        out = renderer.render(counts(), FIXED, generated=NOW)
        self.assertIn("2147", out)
        self.assertIn("580", out)
        self.assertIn("73.0%", out)
        self.assertIn("نسبة المغلق إلى الإجمالي", out)
        self.assertIn("2024-05-01", out)
        self.assertIn("<pre>", out)
        self.assertIn("سطر ثابت أول", out)
        self.assertIn("تعليماتك الدائمة", out)
        self.assertNotIn("work", out.lower())

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            renderer.render(counts(10, 8, 8), FIXED)
        with self.assertRaises(ValueError):
            renderer.render(counts(-1, 0, 0), FIXED)
        with self.assertRaises(ValueError):
            renderer.render(counts(), "   ")
        bad = counts()
        del bad["org_created_at"]
        with self.assertRaises(KeyError):
            renderer.render(bad, FIXED)

    def test_chunking(self):
        body = renderer.render(counts(), FIXED + "\n" + ("سطر طويل للاختبار\n" * 300), generated=NOW)
        parts = renderer.chunk(body)
        self.assertGreater(len(parts), 1)
        for part in parts:
            self.assertLessEqual(len(part), renderer.MAX_CHARS)
        self.assertEqual("".join(p + "\n" for p in parts).replace("\n\n", "\n").count("سطر ثابت أول"), 1)
        with self.assertRaises(ValueError):
            renderer.chunk("x" * (renderer.MAX_CHARS + 1))
        with self.assertRaises(ValueError):
            renderer.chunk("")


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


def receipt(chat_id, topic_id, text, mid=111):
    return {
        "ok": True,
        "result": {
            "message_id": mid,
            "chat": {"id": int(chat_id)},
            "message_thread_id": int(topic_id),
            "text": sender.expected_plain(text),
            "entities": (
                [{"type": "bold"}] * text.count("<b>") + [{"type": "pre"}] * text.count("<pre>")
            ),
        },
    }


CHUNK = "<b>عنوان</b>\n<pre>1  2</pre>\nسطر عادي"


class IncidentRegressionTests(unittest.TestCase):
    """First dispatch (run 40145046180): the rendered body ended with a newline
    on the single-chunk path and the counts-table data row carried trailing
    spaces; Telegram trims these in result.text, so the full plain-text receipt
    check failed after a successful send. Pin the fix: rendered output and the
    wire text must already be in canonical form, and the receipt is verified
    against exactly that form."""

    def test_render_has_canonical_whitespace(self):
        out = renderer.render(counts(), FIXED, generated=NOW)
        self.assertFalse(out.endswith("\n"))
        for line in out.split("\n"):
            self.assertEqual(line, line.rstrip())

    def test_sender_normalizes_wire_text(self):
        captured = {}
        def opener(req, timeout=30):
            import urllib.parse as up
            captured.update(up.parse_qs(req.data.decode()))
            return Response(json.dumps(receipt("-100123", "43", sender.normalize(CHUNK + "   \n"))))
        messy = CHUNK + "   \n\n"
        out = sender.send_pulse("tok", "-100123", "43", [messy], opener=opener)
        self.assertEqual(out, [111])
        self.assertEqual(captured["text"][0], CHUNK)

    def test_mismatch_reports_diff_position(self):
        tampered = receipt("-100123", "43", CHUNK)
        tampered["result"]["text"] = sender.expected_plain(CHUNK)[:-2] + "XY"
        try:
            sender.send_pulse("t", "-100123", "43", [CHUNK], opener=opener_of([tampered]))
            self.fail("must raise")
        except ValueError as e:
            self.assertIn("mismatch at char", str(e))


class SenderTests(unittest.TestCase):
    def test_send_happy(self):
        opener = opener_of([receipt("-100123", "43", CHUNK)])
        out = sender.send_pulse("tok", "-100123", "43", [CHUNK], opener=opener)
        self.assertEqual(out, [111])

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            sender.send_pulse("", "-100123", "43", [CHUNK])
        with self.assertRaises(ValueError):  # not ok
            sender.send_pulse("t", "-100123", "43", [CHUNK], opener=opener_of([{"ok": False}]))
        wrong_chat = receipt("-100999", "43", CHUNK)
        with self.assertRaises(ValueError):
            sender.send_pulse("t", "-100123", "43", [CHUNK], opener=opener_of([wrong_chat]))
        wrong_topic = receipt("-100123", "99", CHUNK)
        with self.assertRaises(ValueError):
            sender.send_pulse("t", "-100123", "43", [CHUNK], opener=opener_of([wrong_topic]))
        tampered = receipt("-100123", "43", CHUNK)
        tampered["result"]["text"] = "نص مختلف"
        with self.assertRaises(ValueError):
            sender.send_pulse("t", "-100123", "43", [CHUNK], opener=opener_of([tampered]))
        no_entities = receipt("-100123", "43", CHUNK)
        no_entities["result"]["entities"] = []
        with self.assertRaises(ValueError):
            sender.send_pulse("t", "-100123", "43", [CHUNK], opener=opener_of([no_entities]))
        with self.assertRaises(ValueError):
            sender.send_pulse("t", "-100123", "43", [""], opener=opener_of([]))


if __name__ == "__main__":
    unittest.main()
