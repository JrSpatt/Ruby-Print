# SPDX-License-Identifier: GPL-3.0-only
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from pypdf import PdfWriter
from server import PrintManager, batches, build_command, make_handler, ThreadingHTTPServer, connection_failed_before_write


OPTIONS = {"device": "MX10", "mode": "floyd_steinberg", "gap": 0,
           "darkness": 0, "trim_side": True, "trim_vertical": True}


def pdf_bytes(pages=5, encrypted=False):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=420, height=595)
    if encrypted:
        writer.encrypt("test-password")
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class FakeManager(PrintManager):
    def __init__(self, *args, fail_at=None, process_delay=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.commands, self.times = [], []
        self.fail_at = fail_at
        self.process_delay = process_delay
        self.entered = threading.Event()

    def _run_process(self, command):
        with self.lock:
            self.active = True
            self.commands.append(command)
            index = len(self.commands)
            self.times.append([time.monotonic(), None])
            self.entered.set()
        time.sleep(self.process_delay)
        with self.lock:
            self.active = None
            self.times[-1][1] = time.monotonic()
        return 1 if self.fail_at == index else 0


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = Path(self.tmp.name) / "engine.exe"
        self.engine.write_bytes(b"test engine")
        self.managers = []

    def tearDown(self):
        for m in self.managers:
            if m.busy():
                m.control("stop")
            if m.worker:
                m.worker.join(3)
        self.tmp.cleanup()

    def manager(self, **kwargs):
        workdir = Path(self.tmp.name) / str(len(self.managers))
        workdir.mkdir()
        m = FakeManager(engine=self.engine, workdir=workdir, **kwargs)
        self.managers.append(m)
        return m

    def run_job(self, m, first=1, last=5):
        m.upload(pdf_bytes(max(last, 5)), "Türkçe kitap (1).pdf")
        m.start({**OPTIONS, "first": first, "last": last})
        m.worker.join(5)
        self.assertFalse(m.worker.is_alive())

    def test_connection_retry_evidence(self):
        clean = ["disconnect state: connected_ms=<none> write_chunks=0 write_bytes=0 last_write=<none>",
                 "Error: Bluetooth connection failed"]
        self.assertTrue(connection_failed_before_write(clean))
        self.assertFalse(connection_failed_before_write([clean[1]]))
        self.assertFalse(connection_failed_before_write([clean[0]]))
        for extra in ["write mode: payload=100", "standard progress: chunks=1/10", "standard write done: bytes=100"]:
            self.assertFalse(connection_failed_before_write(clean + [extra]))
        self.assertFalse(connection_failed_before_write([clean[0].replace("write_bytes=0", "write_bytes=18"), clean[1]]))

    def test_safe_retry_preserves_page_order(self):
        m = self.manager(delay=0)
        original = m._run_process
        def run(command):
            original(command)
            m.retry_safe = len(m.commands) == 1
            return 2 if m.retry_safe else 0
        m._run_process = run
        self.run_job(m, last=5)
        self.assertEqual(m.state, "done")
        self.assertEqual(m.confirmed, 5)
        self.assertEqual([c[c.index("--pdf-pages")+1] for c in m.commands], ["1-2", "1-2", "3-4", "5"])

    def test_retry_limit(self):
        m = self.manager(delay=0)
        original = m._run_process
        def run(command):
            original(command)
            m.retry_safe = True
            return 2
        m._run_process = run
        self.run_job(m)
        self.assertEqual(len(m.commands), 3)
        self.assertEqual(m.confirmed, 0)
        self.assertEqual(m.state, "error")

    def test_stop_cancels_pending_retry(self):
        m = self.manager(delay=0)
        original = m._run_process
        def run(command):
            original(command)
            m.retry_safe = True
            m.control("stop")
            return 2
        m._run_process = run
        self.run_job(m)
        self.assertEqual(len(m.commands), 1)
        self.assertEqual(m.state, "stopped")

    def test_all_142_pages_no_duplicates_or_omissions(self):
        m = self.manager(delay=0)
        self.run_job(m, last=142)
        self.assertEqual(len(m.commands), 71)
        ranges = [c[c.index("--pdf-pages")+1] for c in m.commands]
        self.assertEqual(ranges, [f"{a}-{b}" for a, b in batches(1, 142)])
        self.assertEqual(m.confirmed, 142)
        self.assertEqual(m.state, "done")

    def test_odd_last_page_and_three_second_default(self):
        m = self.manager()
        self.assertEqual(m.delay, 3)
        self.assertEqual(batches(10, 14), [(10, 11), (12, 13), (14, 14)])
        c = build_command(self.engine, "book.pdf", 14, 14, OPTIONS)
        self.assertEqual(c[c.index("--pdf-pages")+1], "14")

    def test_wait_is_after_process_completion(self):
        m = self.manager(delay=.06, process_delay=.025)
        self.run_job(m)
        self.assertEqual(len(m.commands), 3)
        for i in range(1, len(m.times)):
            self.assertGreaterEqual(m.times[i][0] - m.times[i-1][1], .058)

    def test_error_stops_without_retrying(self):
        m = self.manager(delay=0, fail_at=2)
        self.run_job(m)
        self.assertEqual(m.state, "error")
        self.assertEqual(len(m.commands), 2)
        self.assertEqual(m.confirmed, 2)
        self.assertEqual(m.next_page, 3)

    def test_pause_resume_preserves_order(self):
        m = self.manager(delay=.02, process_delay=.08)
        m.upload(pdf_bytes(), "book.pdf")
        m.start({**OPTIONS, "first": 1, "last": 5})
        self.assertTrue(m.entered.wait(1))
        m.control("pause")
        time.sleep(.18)
        self.assertEqual(m.state, "paused")
        self.assertEqual(len(m.commands), 1)
        m.control("resume")
        m.worker.join(3)
        self.assertEqual(m.confirmed, 5)
        self.assertEqual(m.state, "done")

    def test_stop_finishes_current_pair(self):
        m = self.manager(delay=.02, process_delay=.08)
        m.upload(pdf_bytes(), "book.pdf")
        m.start({**OPTIONS, "first": 1, "last": 5})
        self.assertTrue(m.entered.wait(1))
        m.control("stop")
        m.worker.join(3)
        self.assertEqual(m.state, "stopped")
        self.assertEqual(m.confirmed, 2)
        self.assertEqual(m.next_page, 3)
        self.assertEqual(len(m.commands), 1)

    def test_no_shell_and_options_are_exact(self):
        special = 'C:/Users/Örnek & name/a "b".pdf'
        c = build_command(self.engine, special, 10, 11, OPTIONS)
        self.assertEqual(c[-1], special)
        self.assertNotIn("--darkness", c)
        self.assertNotIn("--no-trim-side-margins", c)
        self.assertNotIn("--no-trim-top-bottom-margins", c)
        o = {**OPTIONS, "trim_side": False, "trim_vertical": False, "darkness": 4}
        c = build_command(self.engine, special, 10, 11, o)
        self.assertIn("--no-trim-side-margins", c)
        self.assertIn("--no-trim-top-bottom-margins", c)
        self.assertEqual(c[c.index("--darkness")+1], "4")

    def test_pdf_validation_and_range(self):
        m = self.manager(delay=0)
        self.assertEqual(m.upload(pdf_bytes(7), "x.pdf")["file"]["pages"], 7)
        for a, b in [(0, 3), (4, 3), (1, 8)]:
            with self.assertRaises(ValueError):
                m.start({**OPTIONS, "first": a, "last": b})
        with self.assertRaises(ValueError):
            m.upload(b"Not a PDF", "x.pdf")
        with self.assertRaises(ValueError):
            m.upload(pdf_bytes(encrypted=True), "x.pdf")
        self.assertEqual(m.file["pages"], 7)

    def test_upload_during_job_is_rejected(self):
        m = self.manager(delay=0, process_delay=.1)
        m.upload(pdf_bytes(), "book.pdf")
        m.start({**OPTIONS, "first": 1, "last": 5})
        with self.assertRaises(ValueError):
            m.upload(pdf_bytes(), "other.pdf")


class HttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = PrintManager(workdir=self.tmp.name)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.manager, "test-token"))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_pdf_ranges(self):
        content = pdf_bytes(9)
        self.manager.upload(content, "test.pdf")
        for value, expected in [("bytes=0-99", content[:100]),
                                ("bytes=100-", content[100:]),
                                ("bytes=-25", content[-25:])]:
            req = Request(self.url + "/api/pdf?token=test-token", headers={"Range": value})
            with urlopen(req) as response:
                self.assertEqual(response.status, 206)
                self.assertEqual(response.read(), expected)
                self.assertEqual(response.headers["Accept-Ranges"], "bytes")
        req = Request(self.url + "/api/pdf?token=test-token", headers={"Range": "bytes=999999-"})
        with self.assertRaises(HTTPError) as context:
            urlopen(req)
        self.assertEqual(context.exception.code, 416)

    def test_cancelled_browser_request(self):
        from unittest.mock import patch
        from http.server import BaseHTTPRequestHandler
        handler = object.__new__(make_handler(self.manager, "test-token"))
        for error in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            with patch.object(BaseHTTPRequestHandler, "handle", side_effect=error):
                handler.handle()
                self.assertTrue(handler.close_connection)
        with patch.object(BaseHTTPRequestHandler, "handle", side_effect=RuntimeError):
            with self.assertRaises(RuntimeError):
                handler.handle()

    def test_private_api_and_pagecount(self):
        with self.assertRaises(HTTPError) as context:
            urlopen(self.url+"/api/status")
        self.assertEqual(context.exception.code, 403)
        request = Request(self.url+"/api/upload", data=pdf_bytes(9),
                          headers={"X-Ruby-Token": "test-token", "X-File-Name": "test.pdf"})
        with urlopen(request) as response:
            data = json.load(response)
        self.assertEqual(data["file"]["pages"], 9)
        self.assertNotIn("path", data["file"])
        request = Request(self.url+"/api/status", headers={"Host": "attacker.example", "X-Ruby-Token": "test-token"})
        with self.assertRaises(HTTPError) as context:
            urlopen(request)
        self.assertEqual(context.exception.code, 403)


if __name__ == "__main__":
    unittest.main()
