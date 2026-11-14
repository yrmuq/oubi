#!/usr/bin/env python3
"""Страница oubi: при старте только скачивает модель, разметку и раскладку запускают кнопки."""
import json
import os
import re
import signal
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ===== SETTINGS =====
HERE = Path(__file__).resolve().parent
IMAGES = "/images"
OUTPUT = "/output"
CSV_PATH = "/data/tags.csv"
GENDERS_PATH = "/data/genders.csv"
MODES = {"all", "tag", "sort", "plan"}
PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>oubi</title>
<style>
:root { color-scheme: dark; }
body { margin: 0; font: 16px/1.45 system-ui, sans-serif; background: #161616; color: #eee; }
main { max-width: 820px; margin: 0 auto; padding: 28px 20px 48px; }
h1 { font-size: 28px; margin: 0 0 8px; }
.step { font-size: 20px; margin: 18px 0 10px; }
.bar { height: 14px; background: #2a2a2a; border-radius: 99px; overflow: hidden; }
.bar > div { height: 100%; width: 0; background: #e7b341; }
.detail { color: #aaa; min-height: 1.4em; }
.row { display: flex; flex-wrap: wrap; gap: 10px; margin: 18px 0; }
button { font: inherit; padding: 12px 16px; border: 0; border-radius: 10px; background: #2c4a3e; color: #fff; cursor: pointer; }
button.secondary { background: #333; }
button.danger { background: #6e2e2e; }
button:disabled { opacity: 0.45; cursor: default; }
.warn { color: #e7b341; }
pre { background: #101010; padding: 12px; border-radius: 10px; max-height: 320px; overflow: auto; white-space: pre-wrap; }
</style>
</head>
<body>
<main>
<h1>oubi</h1>
<p id="step" class="step">Открываю…</p>
<div class="bar"><div id="fill"></div></div>
<p id="detail" class="detail"></p>
<div class="row">
  <button id="all">Разметить и разложить</button>
  <button id="tag">Только разметить</button>
  <button id="sort">Только разложить</button>
  <button id="plan" class="secondary">Только план раскладки</button>
  <button id="stop" class="danger" disabled>Остановить</button>
</div>
<p class="warn">Повторная раскладка копирует файлы ещё раз, со следующими номерами. План ничего не копирует.</p>
<p><button id="retry" class="secondary" hidden>Повторить скачивание модели</button></p>
<pre id="log"></pre>
</main>
<script>
const ids = ["all", "tag", "sort", "plan"];
async function tick() {
  const data = await (await fetch("/api/status")).json();
  document.getElementById("step").textContent = data.step || "Жду.";
  document.getElementById("detail").textContent = data.detail || "";
  document.getElementById("fill").style.width = (data.percent || 0) + "%";
  const log = document.getElementById("log");
  log.textContent = (data.log || []).join(String.fromCharCode(10));
  log.scrollTop = log.scrollHeight;
  const busy = data.job_running;
  const ready = data.model === "ready";
  for (const id of ids) {
    const needsModel = id === "all" || id === "tag";
    document.getElementById(id).disabled = busy || (needsModel && !ready);
  }
  document.getElementById("stop").disabled = !busy;
  document.getElementById("retry").hidden = data.model !== "error";
}
async function start(mode) {
  await fetch("/api/job", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({mode})});
  tick();
}
for (const id of ids) document.getElementById(id).onclick = () => start(id);
document.getElementById("stop").onclick = () => fetch("/api/stop", {method: "POST"});
document.getElementById("retry").onclick = () => fetch("/api/download", {method: "POST"});
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""


# ===== READ A LOG LINE =====
def parse_progress(line):
    text = line.strip()
    found = {}
    download = re.search(r"Скачивание:\s+(\d+)%", text)
    if download:
        found["percent"] = int(download.group(1))
        found["detail"] = text
        found["kind"] = "download"
        return found
    tagging = re.search(r"Разметка:\s+(\d+)%", text)
    if tagging:
        found["percent"] = int(tagging.group(1))
        found["detail"] = text
        found["kind"] = "tag"
        return found
    sorting = re.search(r"Раскладка:\s+(\d+)\s+из\s+(\d+)", text)
    if sorting:
        done, total = int(sorting.group(1)), int(sorting.group(2))
        found["percent"] = int(done * 100 / total) if total else 0
        found["detail"] = text
        found["kind"] = "sort"
        return found
    if text.startswith("Сейчас:"):
        found["step"] = text
    elif text.startswith(("Модель скачана", "Модель уже скачана", "Модель загружена", "Готово", "Итого:")):
        found["step"] = text
        if text.startswith(("Готово", "Итого:")):
            found["percent"] = 100
    return found


def flag_on(value):
    return str(value or "").strip() in {"1", "true", "TRUE", "yes", "YES"}


def reject_reason(model_state, job_running, mode):
    if mode not in MODES:
        return "Неизвестная кнопка."
    if job_running:
        return "Уже идёт другая работа."
    if mode in {"tag", "all"} and model_state != "ready":
        return "Сначала должна скачаться модель."
    return None


def preflight(mode):
    if mode in {"tag", "all"} and not os.path.isdir(IMAGES):
        return "Папка картинок не смонтирована в /images."
    batch = os.environ.get("OUBI_BATCH_SIZE", "1")
    if mode in {"tag", "all"} and (not batch.isdigit() or int(batch) < 1):
        return "OUBI_BATCH_SIZE должен быть 1 или больше."
    if mode in {"sort", "plan", "all"} and not os.path.isdir(OUTPUT):
        return "Папка результата не смонтирована в /output."
    if mode in {"sort", "plan"} and not os.path.isfile(CSV_PATH):
        return "Нет tags.csv. Сначала разметка."
    if mode in {"sort", "plan", "all"} and os.environ.get("OUBI_NSFW_FROM", "q") not in {"q", "e"}:
        return "OUBI_NSFW_FROM должен быть q или e."
    return None


# ===== COMMANDS =====
def tag_command():
    command = [
        sys.executable,
        str(HERE / "tag_folder.py"),
        IMAGES,
        "--model",
        os.environ.get("OUBI_MODEL", "pixai-labs/pixai-tagger-v1.0"),
        "--csv",
        CSV_PATH,
        "--batch-size",
        os.environ.get("OUBI_BATCH_SIZE", "1"),
    ]
    if flag_on(os.environ.get("OUBI_NO_TXT")):
        command.append("--no-txt")
    if flag_on(os.environ.get("OUBI_FORCE")):
        command.append("--force")
    return command


def sort_command(plan=False):
    command = [
        sys.executable,
        str(HERE / "sort_images.py"),
        "--csv",
        CSV_PATH,
        "--out",
        OUTPUT,
        "--nsfw-from",
        os.environ.get("OUBI_NSFW_FROM", "q"),
    ]
    if os.path.isfile(GENDERS_PATH):
        command.extend(["--genders", GENDERS_PATH])
    if flag_on(os.environ.get("OUBI_MOVE")):
        command.append("--move")
    if plan:
        command.append("--dry-run")
    if flag_on(os.environ.get("OUBI_COPY_TXT")):
        command.append("--copy-txt")
    return command


class LineTee:
    def __init__(self, app, underlying):
        self.app = app
        self.underlying = underlying
        self.pending = ""

    def write(self, text):
        self.underlying.write(text)
        self.underlying.flush()
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.app.note(line, echo=False)
        return len(text)

    def flush(self):
        self.underlying.flush()


# ===== STATE =====
class App:
    def __init__(self):
        self.lock = threading.Lock()
        self.lines = []
        self.model_state = "downloading"
        self.job_running = False
        self.job_mode = None
        self.phase = "download"
        self.step = "Сейчас: скачиваю модель."
        self.detail = ""
        self.percent = None
        self.proc = None

    def note(self, line, echo=True):
        text = line.strip()
        if not text:
            return
        with self.lock:
            self.lines.append(text)
            del self.lines[:-300]
            parsed = parse_progress(text)
            if "percent" in parsed:
                self.percent = parsed["percent"]
            if "detail" in parsed:
                self.detail = parsed["detail"]
            if "step" in parsed:
                self.step = parsed["step"]
            if "kind" in parsed:
                self.phase = parsed["kind"]
        if echo:
            print(text, flush=True)

    def status(self):
        with self.lock:
            return {
                "model": self.model_state,
                "job_running": self.job_running,
                "job_mode": self.job_mode,
                "phase": self.phase,
                "step": self.step,
                "detail": self.detail,
                "percent": self.percent,
                "log": self.lines[-80:],
            }

    def download(self):
        with self.lock:
            self.model_state = "downloading"
            self.phase = "download"
            self.percent = None
            self.step = "Сейчас: скачиваю модель."
        model = os.environ.get("OUBI_MODEL", "pixai-labs/pixai-tagger-v1.0")
        try:
            if os.path.isdir(model):
                self.note("Сейчас: модель локальная, скачивание не нужно.")
            else:
                from tag_folder import download_model

                previous = sys.stdout
                sys.stdout = LineTee(self, previous)
                try:
                    download_model(model)
                finally:
                    sys.stdout = previous
            with self.lock:
                self.model_state = "ready"
                self.step = "Модель скачана. Разметку и раскладку запускают кнопки."
                self.percent = 100
        except Exception as error:
            self.note(f"Сейчас: не удалось скачать модель. {error}")
            with self.lock:
                self.model_state = "error"

    def retry_download(self):
        with self.lock:
            if self.model_state == "downloading" or self.job_running:
                return False
            self.model_state = "downloading"
        threading.Thread(target=self.download, daemon=True).start()
        return True

    def start(self, mode):
        reason = preflight(mode)
        if reason:
            self.note(f"Сейчас: {reason}")
            return False
        with self.lock:
            if self.job_running:
                return False
            self.job_running = True
            self.job_mode = mode
            self.percent = None
            self.detail = ""
            self.phase = "sort" if mode in {"sort", "plan"} else "tag"
            self.step = "Сейчас: запускаю."
        threading.Thread(target=self._run, args=(mode,), daemon=True).start()
        return True

    def _run(self, mode):
        commands = []
        if mode in {"tag", "all"}:
            commands.append(tag_command())
        if mode in {"sort", "plan", "all"}:
            commands.append(sort_command(plan=mode == "plan"))
        code = 0
        try:
            for command in commands:
                if not self.job_running:
                    code = -15
                    break
                with self.lock:
                    if not self.job_running:
                        code = -15
                        break
                self.proc = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    start_new_session=True,
                )
                for line in self.proc.stdout:
                    self.note(line)
                code = self.proc.wait()
                self.proc = None
                if code != 0:
                    break
        finally:
            with self.lock:
                self.job_running = False
                self.job_mode = None
                self.proc = None
            if code == 0:
                self.note("Сейчас: работа закончилась.")
            elif code < 0:
                self.note("Сейчас: работу остановили.")
            else:
                self.note(f"Сейчас: работа завершилась с ошибкой {code}.")

    def stop(self):
        with self.lock:
            proc = self.proc
            self.job_running = False
        if proc is None or proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, fmt, *args):
        return

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
            return
        if self.path == "/api/status":
            self._send(200, self.app.status())
            return
        self._send(404, {"error": "нет такой страницы"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(min(length, 4096)) if length else b"{}"
        try:
            payload = json.loads(raw.decode() or "{}")
        except json.JSONDecodeError:
            self._send(400, {"error": "непонятный запрос"})
            return
        if self.path == "/api/stop":
            self.app.stop()
            self._send(200, {"ok": True})
            return
        if self.path == "/api/download":
            if not self.app.retry_download():
                self._send(409, {"error": "скачивание уже идёт"})
                return
            self._send(200, {"ok": True})
            return
        if self.path != "/api/job":
            self._send(404, {"error": "нет такой страницы"})
            return
        mode = payload.get("mode")
        state = self.app.status()
        reason = reject_reason(state["model"], state["job_running"], mode)
        if reason:
            self._send(409, {"error": reason})
            return
        if not self.app.start(mode):
            self._send(409, {"error": self.app.status()["step"]})
            return
        self._send(200, {"ok": True})


def main():
    app = App()
    Handler.app = app
    port = int(os.environ.get("OUBI_LISTEN", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=app.download, daemon=True).start()
    print(f"Сейчас: страница на порту {port}.", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
