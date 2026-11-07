# SPDX-License-Identifier: GPL-3.0-only
"""Ruby Print: local UI around the unmodified TiMini Print CLI. No cloud services."""
from __future__ import annotations

import argparse
import base64
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit
import webbrowser

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ROOT / "engine" / "TiMini-Print-Command-Line-Windows-x86_64.exe"
MAX_UPLOAD = 120 * 1024 * 1024
MODES = {"floyd_steinberg", "threshold", "atkinson", "bayer_4", "bayer_8"}


def batches(first, last):
    return [(n, min(n + 1, last)) for n in range(first, last + 1, 2)]


def build_command(engine, path, first, last, options):
    command = [str(engine), "--bluetooth", options["device"], "--pdf-pages",
               f"{first}-{last}" if first != last else str(first),
               "--page-gap", str(options["gap"]), "--image-mode", options["mode"], "-v"]
    if not options["trim_side"]:
        command.append("--no-trim-side-margins")
    if not options["trim_vertical"]:
        command.append("--no-trim-top-bottom-margins")
    if options["darkness"]:
        command += ["--darkness", str(options["darkness"])]
    return command + [str(path)]


def connection_failed_before_write(lines):
    # Retry only with explicit evidence that this attempt sent zero bytes.
    text = "\n".join(lines)
    return ("Error: Bluetooth connection" in text
            and re.search(r"disconnect state:.*write_chunks=0 write_bytes=0(?: |$)", text) is not None
            and "write mode:" not in text
            and "standard progress:" not in text
            and "standard write done:" not in text)


class PrintManager:
    def __init__(self, engine=ENGINE, delay=3.0, workdir=None):
        self.engine = Path(engine)
        self.delay = delay  # Fixed at 3 s in production. Constructor injection is for tests.
        self.workdir = Path(workdir or tempfile.mkdtemp(prefix="ruby-print-"))
        self.lock = threading.RLock()
        self.condition = threading.Condition(self.lock)
        self.file = None
        self.state = "idle"
        self.message = "Bir PDF seçerek başla."
        self.options = None
        self.pause_requested = False
        self.stop_requested = False
        self.active = None
        self.confirmed = 0  # CLI acknowledgements, NOT a claim of physical print verification.
        self.total = 0
        self.current = None
        self.next_page = None
        self.countdown = 0
        self.started = None
        self.finished = None
        self.log = deque(maxlen=1200)
        self.log_id = 0
        self.worker = None
        self.last_error = None
        self.retry_safe = False

    def add_log(self, line, kind="engine"):
        with self.lock:
            self.log_id += 1
            self.log.append({"id": self.log_id, "time": time.strftime("%H:%M:%S"),
                             "text": line.rstrip()[:5000], "kind": kind})

    def busy(self):
        return self.state in {"printing", "waiting", "paused", "pausing", "stopping"}

    def upload(self, content, name):
        with self.lock:
            if self.busy():
                raise ValueError("PDF değiştirmeden önce baskıyı durdur.")
            if not content.startswith(b"%PDF-"):
                raise ValueError("Geçerli bir PDF dosyası seç.")
            candidate = self.workdir / (secrets.token_hex(12) + ".pdf")
            candidate.write_bytes(content)
            reader = None
            try:
                reader = PdfReader(candidate, strict=False)
                if reader.is_encrypted:
                    raise ValueError("Şifreli PDF desteklenmiyor. Önce şifresiz bir kopyasını kaydet.")
                count = len(reader.pages)
                if count < 1:
                    raise ValueError("PDF içinde sayfa bulunamadı.")
                if count > 20000:
                    raise ValueError("En fazla 20.000 sayfa destekleniyor.")
                page = reader.pages[0]
                width, height = float(page.mediabox.width), float(page.mediabox.height)
                reader.close()
            except Exception as exc:
                if reader is not None:
                    reader.close()
                candidate.unlink(missing_ok=True)
                if isinstance(exc, ValueError):
                    raise
                raise ValueError("PDF okunamadı. Dosyayı yeniden PDF olarak kaydedip dene.") from exc
            if self.file:
                self.file["path"].unlink(missing_ok=True)
            clean_name = str(name).replace("\\", "/").split("/")[-1][:180]
            self.file = {"path": candidate, "name": clean_name, "pages": count,
                         "size": len(content), "width": width, "height": height}
            self.state = "ready"
            self.message = "PDF hazır. Baskı ayarlarını kontrol edebilirsin."
            self.confirmed = self.total = 0
            self.current = self.next_page = self.last_error = None
            self.started = self.finished = None
            self.log.clear()
            self.add_log(f"PDF içeri alındı · {clean_name} · {count} sayfa", "info")
            return self.snapshot()

    def start(self, request):
        with self.lock:
            if self.busy():
                raise ValueError("Devam eden bir baskı var.")
            if not self.file:
                raise ValueError("Önce bir PDF seç.")
            if not self.engine.is_file():
                raise ValueError("TiMini motoru bulunamadı. Paketi ZIP içinden tamamen çıkarıp tekrar aç.")
            try:
                first, last = int(request["first"]), int(request["last"])
                gap, darkness = int(request.get("gap", 0)), int(request.get("darkness", 0))
            except (KeyError, TypeError, ValueError):
                raise ValueError("Sayfa aralığı ve baskı değerleri sayı olmalı.")
            if not 1 <= first <= last <= self.file["pages"]:
                raise ValueError(f"Sayfa aralığı 1–{self.file['pages']} içinde olmalı.")
            if request.get("mode") not in MODES or not 0 <= gap <= 30 or not 0 <= darkness <= 5:
                raise ValueError("Baskı ayarları geçersiz.")
            device = str(request.get("device", "MX10")).strip()
            if not device or len(device) > 120 or any(ord(c) < 32 for c in device):
                raise ValueError("Geçerli bir Bluetooth adı veya adresi yaz.")
            options = {"device": device, "mode": request["mode"], "gap": gap,
                       "darkness": darkness, "trim_side": bool(request.get("trim_side", True)),
                       "trim_vertical": bool(request.get("trim_vertical", True))}
            self.options = options
            self.pause_requested = self.stop_requested = False
            self.state, self.message = "printing", "Baskı hazırlanıyor…"
            self.total, self.confirmed = last - first + 1, 0
            self.current, self.next_page = None, first
            self.last_error = None
            self.started, self.finished = time.time(), None
            self.add_log(f"Baskı başladı · {first}–{last} · ikişerli · {self.delay:g} sn bekleme", "info")
            self.worker = threading.Thread(target=self._run, args=(self.file["path"], first, last, options), daemon=True)
            self.worker.start()
            return self.snapshot()

    def control(self, action):
        with self.condition:
            if not self.busy():
                raise ValueError("Aktif bir baskı yok.")
            if action == "pause":
                if self.stop_requested:
                    raise ValueError("Baskı zaten durduruluyor.")
                self.pause_requested = True
                if self.active:
                    self.state = "pausing"
                    self.message = "Bu ikili bitince duraklatılacak."
                self.add_log("Duraklatma istendi; mevcut ikili kesilmeyecek.", "info")
            elif action == "resume":
                if self.stop_requested:
                    raise ValueError("Baskı zaten durduruluyor.")
                self.pause_requested = False
                if self.state in {"paused", "pausing"}:
                    self.state = "printing" if self.active else "waiting"
                    self.message = "Devam ediliyor…"
                self.add_log("Baskıya devam ediliyor.", "info")
            elif action == "stop":
                self.stop_requested = True
                self.pause_requested = False
                self.state = "stopping"
                self.message = "Mevcut ikili bitince baskı duracak."
                self.add_log("Durdurma istendi; yeni ikili gönderilmeyecek.", "info")
            else:
                raise ValueError("Bilinmeyen işlem.")
            self.condition.notify_all()
            return self.snapshot()

    def _run_process(self, command):
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                   creationflags=flags, env={**os.environ, "TIMINIPRINT_NO_UPDATE_CHECK": "1",
                                                          "PYTHONIOENCODING": "utf-8"})
        with self.lock:
            self.active = process
        attempt_lines = []
        def read_output():
            for line in process.stdout:
                if line.strip():
                    attempt_lines.append(line.rstrip())
                    self.add_log(line)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            result = process.wait(timeout=600)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError("TiMini 10 dakika içinde tamamlanmadı. Yazıcıyı ve bağlantıyı kontrol et.")
        finally:
            reader.join(timeout=3)
            process.stdout.close()
            with self.lock:
                self.active = None
        self.retry_safe = result != 0 and connection_failed_before_write(attempt_lines)
        return result

    def _wait_until_ready(self):
        with self.condition:
            while self.pause_requested and not self.stop_requested:
                self.state, self.message = "paused", "Duraklatıldı. Devam etmek için düğmeye bas."
                self.condition.wait()
            return not self.stop_requested

    def _cooldown(self):
        deadline = time.monotonic() + self.delay
        with self.condition:
            while not self.stop_requested:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.countdown = 0
                    return True
                self.countdown = round(remaining, 1)
                self.state = "paused" if self.pause_requested else "waiting"
                self.message = "Duraklatıldı." if self.pause_requested else "Yazıcı için 3 saniye bekleniyor…"
                self.condition.wait(timeout=min(0.1, remaining))
            return False

    def _run(self, path, first, last, options):
        try:
            jobs = batches(first, last)
            for index, (a, b) in enumerate(jobs):
                if not self._wait_until_ready():
                    break
                with self.lock:
                    # Stop/pause can arrive between the condition check and process launch.
                    if self.stop_requested:
                        break
                    self.current = [a, b]
                    self.next_page = a
                    self.state, self.message = "printing", f"{a}–{b}. sayfalar yazıcıya gönderiliyor…"
                    self.add_log(f"İkili {index+1}/{len(jobs)} · sayfa {a}–{b}", "info")
                result = 0
                for attempt in range(3):
                    self.retry_safe = False
                    result = self._run_process(build_command(self.engine, path, a, b, options))
                    if result == 0 or not self.retry_safe or attempt == 2:
                        break
                    self.add_log(f"Bağlantı kurulamadı; veri gönderilmedi. Yeniden deneme {attempt + 1}/2 · {a}–{b}.", "info")
                    if not self._cooldown() or not self._wait_until_ready():
                        break
                if result != 0 and self.retry_safe and self.stop_requested:
                    break
                if result != 0:
                    raise RuntimeError(f"TiMini hata kodu {result} döndürdü. {a}–{b}. sayfaları kontrol et; baskı durduruldu.")
                with self.lock:
                    self.confirmed += b - a + 1
                    self.next_page = b + 1 if b < last else None
                    self.add_log(f"TiMini işlemi tamamlandı · {a}–{b}. sayfalar", "success")
                if index < len(jobs) - 1 and not self._cooldown():
                    break
            with self.lock:
                if self.confirmed == self.total:
                    self.state = "done"
                    self.message = "Tüm işler gönderildi. Çıktının eksiksiz olduğunu kontrol et."
                else:
                    self.state = "stopped"
                    self.message = f"Durduruldu. Sonraki sayfa: {self.next_page}."
                self.finished = time.time()
                self.countdown = 0
                self.add_log(self.message, "info")
        except Exception as exc:
            with self.lock:
                self.state, self.message = "error", str(exc)
                self.last_error = str(exc)
                self.finished = time.time()
                self.countdown = 0
                self.add_log(str(exc), "error")

    def snapshot(self, since=0):
        with self.lock:
            public_file = {k: v for k, v in self.file.items() if k != "path"} if self.file else None
            return {"state": self.state, "message": self.message, "file": public_file,
                    "engine_ready": self.engine.is_file(), "confirmed": self.confirmed, "total": self.total,
                    "current": self.current, "next_page": self.next_page, "countdown": self.countdown,
                    "pause_requested": self.pause_requested, "stop_requested": self.stop_requested,
                    "started": self.started, "finished": self.finished,
                    "logs": [entry for entry in self.log if entry["id"] > since],
                    "log_id": self.log_id, "error": self.last_error}


def make_handler(manager, token):
    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # Browser cancelled a local request (e.g. PDF navigation).
                self.close_connection = True

        def log_message(self, *_args):
            pass

        def headers_common(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Frame-Options", "SAMEORIGIN")

        def json_response(self, data, status=200):
            body = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.headers_common()
            self.end_headers()
            self.wfile.write(body)

        def valid_host(self):
            return self.headers.get("Host") in {f"127.0.0.1:{self.server.server_port}",
                                                f"localhost:{self.server.server_port}"}

        def authorized(self):
            query_token = parse_qs(urlsplit(self.path).query).get("token", [""])[0]
            supplied = self.headers.get("X-Ruby-Token", query_token)
            return self.valid_host() and secrets.compare_digest(supplied, token)

        def do_GET(self):
            path = urlsplit(self.path).path
            if not self.valid_host():
                return self.json_response({"error": "Geçersiz adres."}, 403)
            if path == "/":
                html = (ROOT / "app" / "index.html").read_text(encoding="utf-8").replace("__RUBY_TOKEN__", token)
                body = html.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.headers_common()
                self.end_headers()
                return self.wfile.write(body)
            if not self.authorized():
                return self.json_response({"error": "Yetkisiz istek."}, 403)
            if path == "/api/status":
                try:
                    since = int(parse_qs(urlsplit(self.path).query).get("since", [0])[0])
                except ValueError:
                    since = 0
                return self.json_response(manager.snapshot(since))
            if path == "/api/pdf":
                with manager.lock:
                    if not manager.file:
                        return self.json_response({"error": "PDF yok."}, 404)
                    data = manager.file["path"].read_bytes()
                total = len(data)
                start, end, partial = 0, total - 1, False
                range_header = self.headers.get("Range")
                if range_header:
                    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
                    if match and any(match.groups()):
                        left, right = match.groups()
                        if left:
                            start = int(left)
                            end = min(int(right), total - 1) if right else total - 1
                        else:
                            start = max(0, total - int(right))
                        if start > end or start >= total:
                            self.send_response(416)
                            self.send_header("Content-Range", f"bytes */{total}")
                            self.send_header("Content-Length", "0")
                            self.headers_common()
                            self.end_headers()
                            return
                        partial = True
                        data = data[start:end + 1]
                self.send_response(206 if partial else 200)
                self.send_header("Accept-Ranges", "bytes")
                if partial:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", 'inline; filename="preview.pdf"')
                self.send_header("Content-Length", str(len(data)))
                self.headers_common()
                self.end_headers()
                return self.wfile.write(data)
            return self.json_response({"error": "Bulunamadı."}, 404)

        def do_POST(self):
            if not self.authorized():
                return self.json_response({"error": "Yetkisiz istek."}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                route = urlsplit(self.path).path
                limit = MAX_UPLOAD if route == "/api/upload" else 8192
                if length <= 0 or length > limit:
                    raise ValueError("Dosya çok büyük. PDF en fazla 120 MB olabilir.")
                content = self.rfile.read(length)
                if len(content) != length:
                    raise ValueError("Dosya aktarımı tamamlanmadı.")
                if route == "/api/upload":
                    from urllib.parse import unquote
                    result = manager.upload(content, unquote(self.headers.get("X-File-Name", "belge.pdf")))
                else:
                    request = json.loads(content)
                    if route == "/api/start":
                        result = manager.start(request)
                    elif route in {"/api/pause", "/api/resume", "/api/stop"}:
                        result = manager.control(route.split("/")[-1])
                    elif route == "/api/shutdown":
                        with manager.lock:
                            if manager.busy():
                                raise ValueError("Kapatmadan önce baskıyı durdur ve ikilinin bitmesini bekle.")
                        result = {"closed": True}
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                    else:
                        return self.json_response({"error": "Bulunamadı."}, 404)
                self.json_response(result)
            except (ValueError, TypeError, KeyError) as exc:
                self.json_response({"error": str(exc)}, 400)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                raise
            except Exception:
                self.json_response({"error": "İşlem tamamlanamadı. Uygulamayı yeniden açıp dene."}, 500)
    return Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args()
    manager = PrintManager()
    token = secrets.token_hex(32)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(manager, token))
    server.daemon_threads = True
    url = f"http://127.0.0.1:{server.server_port}"
    print(f"Ruby Print: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if manager.busy():
            manager.control("stop")
            if manager.worker:
                manager.worker.join(timeout=605)
        server.server_close()
        shutil.rmtree(manager.workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
