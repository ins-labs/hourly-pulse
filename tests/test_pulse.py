import datetime as dt
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import collector
import crypto_util
import renderer
import work_source
import sender
import work_table

NOW = dt.datetime.fromisoformat("2026-10-25T02:07:00+03:00")


class Response(io.StringIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def opener_of(bodies):
    stream = iter(bodies)
    return lambda *a, **k: Response(json.dumps(next(stream)))


def counts(t=2147, o=580, c=1567):
    return {"total": t, "open": o, "closed": c, "snapshot_at": NOW.isoformat()}


def work(rows=None):
    return {
        "rows": rows if rows is not None else [{"repo": "alpha", "open": 5, "closed_24h": 2}],
        "window_since": "2026-10-24T02:07:00Z",
        "snapshot_at": NOW.isoformat(),
    }


FIXED = "سطر ثابت أول\nسطر ثابت ثان"


def curated(rows=None, snap="2026-10-25T02:06:00+03:00"):
    return {
        "snapshot_at": snap,
        "rows": rows if rows is not None else [
            {"label": "عمل أول", "status": "in_progress"},
            {"label": "عمل ثان", "status": "parked"},
        ],
    }


class Collector(unittest.TestCase):
    def test_ok(self):
        bodies = [{"total_count": n, "incomplete_results": False} for n in (2147, 580, 1567)]
        out = collector.collect_counts("T", "some-org", opener=opener_of(bodies), now=NOW)
        self.assertEqual((out["total"], out["open"], out["closed"]), (2147, 580, 1567))

    def test_fail_closed(self):
        for body in [
            {"total_count": 3, "incomplete_results": True},
            {"total_count": True, "incomplete_results": False},
            {"total_count": -1, "incomplete_results": False},
            {},
        ]:
            with self.assertRaises(ValueError):
                collector.collect_counts("T", "some-org", opener=opener_of([body]), now=NOW)

    def test_mismatch(self):
        bodies = [{"total_count": n, "incomplete_results": False} for n in (10, 8, 8)]
        with self.assertRaises(ValueError):
            collector.collect_counts("T", "some-org", opener=opener_of(bodies), now=NOW)

    def test_missing_binding(self):
        with self.assertRaises(ValueError):
            collector.collect_counts("", "some-org")
        with self.assertRaises(ValueError):
            collector.collect_counts("T", "")


class WorkTable(unittest.TestCase):
    def test_ok(self):
        bodies = [
            [{"name": "alpha", "archived": False}, {"name": "beta", "archived": False}, {"name": "old", "archived": True}],
            {"total_count": 5, "incomplete_results": False},
            {"total_count": 2, "incomplete_results": False},
            {"total_count": 7, "incomplete_results": False},
            {"total_count": 1, "incomplete_results": False},
        ]
        out = work_table.collect_work_table("T", "some-org", opener=opener_of(bodies), now=dt.datetime(2026, 10, 25, 2, 7, tzinfo=dt.timezone.utc))
        self.assertEqual([r["repo"] for r in out["rows"]], ["alpha", "beta"])
        self.assertEqual(out["rows"][0], {"repo": "alpha", "open": 5, "closed_24h": 2})
        self.assertEqual(out["rows"][1], {"repo": "beta", "open": 7, "closed_24h": 1})

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            work_table.collect_work_table("T", "some-org", opener=opener_of([[]]))
        with self.assertRaises(ValueError):
            work_table.collect_work_table("T", "some-org", opener=opener_of([[{"name": "alpha"}]]))
        with self.assertRaises(ValueError):
            work_table.collect_work_table("", "some-org")


class Renderer(unittest.TestCase):
    def test_render_markers(self):
        out = renderer.render(counts(), work(), curated(), FIXED, generated=NOW)
        self.assertIn("2147", out)
        self.assertIn("73.0%", out)
        self.assertIn("alpha", out)
        self.assertIn("<pre>", out)
        self.assertIn("سطر ثابت أول", out)
        self.assertIn("عمل أول", out)
        self.assertIn("متوقف بأمرك", out)
        self.assertIn("02:06", out)
        self.assertNotIn("لم تُحدَّث منذ", out)

    def test_stale_curated_visible(self):
        out = renderer.render(counts(), work(), curated(snap="2026-10-23T02:06:00+03:00"), FIXED, generated=NOW)
        self.assertIn("لم تُحدَّث منذ أكثر من", out)

    def test_fail_closed(self):
        with self.assertRaises(ValueError):
            renderer.render(counts(10, 8, 8), work(), curated(), FIXED)
        with self.assertRaises(ValueError):
            renderer.render(counts(), work(), curated(), "  ")
        with self.assertRaises(ValueError):
            renderer.render(counts(), {"rows": []}, curated(), FIXED)
        with self.assertRaises(ValueError):
            renderer.render(counts(), work([{"repo": "x", "open": -1, "closed_24h": 0}]), curated(), FIXED)
        with self.assertRaises(ValueError):
            renderer.render(counts(), work(), curated([{"label": "x", "status": "awake"}]), FIXED)
        with self.assertRaises(ValueError):
            renderer.render(counts(), work(), curated([{"label": "x", "status": "pending"}, {"label": "x", "status": "pending"}]), FIXED)
        with self.assertRaises(ValueError):
            renderer.render(counts(), work(), curated([{"label": "a|b", "status": "pending"}]), FIXED)
        with self.assertRaises(ValueError):
            renderer.render(counts(), work(), {"rows": [], "snapshot_at": "2026-10-25T02:06:00+03:00"}, FIXED)

    def test_chunk_boundaries(self):
        line = "سطر " + "x" * 200
        body = "\n".join([line] * 25)
        parts = renderer.chunk(body, limit=1000)
        self.assertTrue(all(len(p) <= 1000 for p in parts))
        with self.assertRaises(ValueError):
            renderer.chunk("x" * 5000, limit=1000)
        self.assertEqual(renderer.chunk("short"), ["short"])


class Crypto(unittest.TestCase):
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


class Sender(unittest.TestCase):
    def receipt(self, text, chat_id="-100123", topic=43, mid=901, entities=None):
        return {
            "ok": True,
            "result": {
                "message_id": mid,
                "chat": {"id": int(chat_id)},
                "message_thread_id": topic,
                "text": sender.expected_plain(text),
                "entities": entities if entities is not None else [
                    {"type": "bold", "offset": 0, "length": 6},
                    {"type": "pre", "offset": 10, "length": 20},
                ],
            },
        }

    def test_ok(self):
        seen = {}

        def opener(request, timeout=30):
            seen["body"] = request.data.decode()
            text = "<b>النبضة</b>\n<pre>row</pre>"
            return Response(json.dumps(self.receipt(text)))

        ids = sender.send_pulse("BT", "-100123", "43", ["<b>النبضة</b>\n<pre>row</pre>"], opener=opener)
        self.assertEqual(ids, [901])
        self.assertIn("message_thread_id=43", seen["body"])
        self.assertIn("parse_mode=HTML", seen["body"])

    def test_fail_closed(self):
        text = "<b>النبضة</b>\n<pre>row</pre>"
        good = self.receipt(text)
        variants = [
            {"ok": False},
            {"ok": True, "result": {}},
        ]
        for wrong_field, wrong_value in [
            ("chat", {"id": -999}),
            ("message_thread_id", 44),
            ("text", "مختلف"),
            ("entities", [{"type": "bold", "offset": 0, "length": 6}]),
        ]:
            bad = json.loads(json.dumps(good))
            bad["result"][wrong_field] = wrong_value
            variants.append(bad)
        for body in variants:
            with self.assertRaises(ValueError):
                sender.send_pulse("BT", "-100123", "43", [text], opener=opener_of([body]))
        with self.assertRaises(ValueError):
            sender.send_pulse("", "-100123", "43", [text])

    def test_expected_plain(self):
        self.assertEqual(sender.expected_plain("<b>a</b> <pre>t</pre>"), "a t")
        self.assertEqual(sender.expected_entities("<b>a</b><pre>t</pre>"), {"bold": 1, "pre": 1})


class MainBinding(unittest.TestCase):
    def test_missing_env_fails_before_any_call(self):
        import pulse_main
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                pulse_main.main()
            self.assertIn("PULSE_GH_READ_TOKEN", str(ctx.exception))
            self.assertIn("PULSE_TEXT_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
