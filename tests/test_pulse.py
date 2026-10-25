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
        "org": "some-org",
        "org_created_at": ORG_CREATED,
        "snapshot_at": NOW.isoformat(),
    }


def count_body(n):
    return {"total_count": n, "incomplete_results": False, "items": []}


class CollectorTests(unittest.TestCase):
    def test_counts_happy(self):
        opener = opener_of([count_body(10), count_body(4), count_body(6), {"created_at": ORG_CREATED}])
        out = collector.collect_counts("t", "some-org", opener=opener, now=NOW)
        self.assertEqual((out["total"], out["open"], out["closed"], out["org"]), (10, 4, 6, "some-org"))
        self.assertTrue(out["org_created_at"].startswith("2024-05-01"))

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            collector.collect_counts("", "org")
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "")
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=opener_of([count_body(10), count_body(4), count_body(4)]))
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=opener_of([{"total_count": 1, "incomplete_results": True}]))
        with self.assertRaises(ValueError):
            collector.collect_counts("t", "o", opener=opener_of([count_body(2), count_body(1), count_body(1), {"login": "o"}]))


class RendererTests(unittest.TestCase):
    def test_blocks_structure(self):
        blocks = renderer.render_blocks(counts(), FIXED, generated=NOW)
        types = [b["type"] for b in blocks]
        self.assertEqual(types, ["heading", "paragraph", "heading", "table", "paragraph", "heading", "paragraph", "paragraph"])
        self.assertEqual(blocks[0]["text"], "النبضة")
        self.assertEqual(blocks[2]["text"], "مهام some-org على GitHub")
        table = blocks[3]
        self.assertTrue(table["is_bordered"])
        self.assertEqual(len(table["cells"]), 2)
        headers = table["cells"][0]
        self.assertEqual([c["text"] for c in headers], ["الإجمالي", "المفتوح", "نسبة المغلق إلى الإجمالي"])
        for c in headers:
            self.assertTrue(c["is_header"])
            self.assertEqual(c["align"], "center")
            self.assertEqual(c["valign"], "middle")
        data = table["cells"][1]
        self.assertEqual([c["text"] for c in data], ["2147", "580", "73.0%"])
        for c in data:
            self.assertNotIn("is_header", c)
            self.assertEqual(c["align"], "center")
            self.assertEqual(c["valign"], "middle")
        self.assertIn("2024-05-01", blocks[4]["text"])
        self.assertEqual(blocks[6]["text"], "سطر ثابت أول\nسطر ثابت ثان")
        self.assertEqual(blocks[7]["text"], "إعداد هذه النسخة لا يثبت نشرها أو وصولها.")

    def test_verbatim_paragraph_split(self):
        text = "فقرة أولى\nسطرها الثاني\n\nفقرة ثانية\n\n\nفقرة ثالثة"
        blocks = renderer.render_blocks(counts(), text, generated=NOW)
        # exact paragraph order against the actual renderer contract:
        # timestamps, coverage disclaimer, fixed text verbatim, footer
        paras = [b["text"] for b in blocks if b["type"] == "paragraph"]
        self.assertEqual(len(paras), 6)
        self.assertTrue(paras[0].startswith("وقت قراءة الأرقام: "))
        self.assertIn("القاهرة", paras[0])
        self.assertTrue(paras[1].startswith("إشيوز GitHub فقط (لا طلبات دمج)"))
        self.assertEqual(paras[2:-1], ["فقرة أولى\nسطرها الثاني", "فقرة ثانية", "فقرة ثالثة"])
        self.assertEqual(paras[-1], "إعداد هذه النسخة لا يثبت نشرها أو وصولها.")

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            renderer.render_blocks(counts(10, 8, 8), FIXED)
        with self.assertRaises(ValueError):
            renderer.render_blocks(counts(-1, 0, 0), FIXED)
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
        tampered["result"]["rich_message"]["blocks"][3]["cells"][1][0]["text"] = "9999"
        with self.assertRaises(ValueError):
            sender.send_rich("t", "-100123", "43", self.blocks, opener=opener_of([tampered]))
        bad_align = rich_receipt("-100123", "43", self.blocks)
        bad_align["result"]["rich_message"]["blocks"][3]["cells"][0][0]["align"] = "left"
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
