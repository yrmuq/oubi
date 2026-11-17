"""Install and verify an optional GPU Python environment in the data volume."""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

PYPI_INDEX = "https://pypi.org/simple"
REQUIREMENTS = str(Path(__file__).with_name("requirements.txt"))
PIP_PROGRESS = re.compile(r"Progress (\d+) of (\d+)")
PIP_FILE = re.compile(r"Downloading ([\w.+-]+\.(?:whl(?:\.metadata)?|tar\.gz))\b")
GPU_BUSY_STATES = {"installing", "checking", "removing"}


class GpuError(Exception):
    def __init__(self, message_id):
        self.message_id = message_id
        super().__init__(message_id)


def gpu_smoke_test(torch, index, error):
    from transformers import pipeline

    if not callable(pipeline):
        raise GpuError(error)
    matrix = torch.ones((32, 32), device=f"cuda:{index}", dtype=torch.float16)
    if (matrix @ matrix)[0, 0].item() != 32:
        raise GpuError(error)
    return torch.cuda.get_device_name(index)


class GpuRuntime:
    def __init__(self, root):
        self.root = Path(root)
        self.lock = threading.Lock()
        self.install = {"state": "idle"}
        self.notify = None
        self.users = {}

    def _marker(self, target, gpu):
        name = f"gpu-name-{gpu}" if gpu else "gpu-name"
        return self.environment(target) / name

    def installed(self, target, gpu=None):
        try:
            if not (self.environment(target) / "bin/python").is_file():
                return ""
            return self._marker(target, gpu).read_text().strip()
        except OSError:
            return ""

    def python_for(self, target, gpu=None):
        if not self.installed(target, gpu):
            raise GpuError(self.missing_runtime)
        return str(self.environment(target) / "bin/python")

    def acquire(self, target, gpu=None):
        with self.lock:
            if self.install["state"] == "removing" and self.install["arch"] == target:
                raise GpuError(120)
            python = self.python_for(target, gpu)
            self.users[target] = self.users.get(target, 0) + 1
            return python

    def release(self, target):
        with self.lock:
            if self.users[target] == 1:
                del self.users[target]
            else:
                self.users[target] -= 1

    def check_removable(self, target, gpu):
        if self.users.get(target):
            raise GpuError(233)
        if not self.installed(target, gpu):
            raise GpuError(237)

    @staticmethod
    def check_platform():
        if platform.system() != "Linux" or platform.machine() != "x86_64" or sys.version_info[:2] != (3, 12):
            raise GpuError(119)

    def packages_ready(self, target):
        runtime = self.environment(target)
        return (runtime / "packages-ready").is_file() and (runtime / "bin/python").is_file()

    def download_size(self, target):
        return 0 if self.packages_ready(target) else self.download_bytes[target]

    def launch_install(self, target, gpu=None, *, notify=None):
        self.check_platform()
        with self.lock:
            if self.install["state"] in GPU_BUSY_STATES:
                raise GpuError(120)
            if self.installed(target, gpu):
                return
            self.notify = notify
            self.install = {"state": "installing", "arch": target, "gpu": gpu,
                            "expected": self.download_size(target), "downloaded": 0, "stage": 146}
        threading.Thread(target=self._install, args=(target, gpu), daemon=True).start()

    def launch_uninstall(self, target, gpu, *, notify):
        with self.lock:
            if self.install["state"] in GPU_BUSY_STATES:
                raise GpuError(120)
            self.check_removable(target, gpu)
            self.install = {"state": "removing", "arch": target, "gpu": gpu}
        try:
            threading.Thread(target=self._uninstall, args=(target, gpu, notify), daemon=True).start()
        except RuntimeError as error:
            self._update(state="error", error=236)
            raise GpuError(236) from error

    def installation_status(self):
        with self.lock:
            return dict(self.install)

    def _update(self, **values):
        with self.lock:
            self.install.update(values)

    def _event(self, message_id, **values):
        if self.notify:
            self.notify(message_id, **values)

    def _stage(self, message_id, **values):
        self._update(stage=message_id, transfer=None, **values)
        self._event(message_id)

    def _pip(self, python, *args):
        command = [
            python, "-u", "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input",
            "--no-color", "install", "--only-binary=:all:", "--no-cache-dir", "--progress-bar", "raw", *args,
        ]
        downloaded = self.installation_status().get("downloaded", 0)
        filename = ""
        amount = 0
        started = last_report = time.monotonic()
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
            for line in proc.stdout:
                line = line.strip()
                if match := PIP_PROGRESS.fullmatch(line):
                    current, total = map(int, match.groups())
                    now = time.monotonic()
                    amount = current
                    if filename and total:
                        speed = current / max(now - started, 0.1)
                        transfer = {"file": filename, "received": current, "total": total, "speed": speed}
                        self._update(downloaded=downloaded + current, transfer=transfer)
                        if now - last_report >= 2:
                            self._event(149, transfer=transfer, downloaded=downloaded + current)
                            last_report = now
                    continue
                print(line, file=sys.__stdout__, flush=True)
                if match := PIP_FILE.match(line):
                    downloaded += amount
                    amount = 0
                    filename = match[1]
                    started = time.monotonic()
                    self._update(downloaded=downloaded, transfer=None, stage=148)
                    if not filename.endswith(".metadata"):
                        self._event(147, file=filename)
                elif line.startswith("Installing collected packages:"):
                    downloaded += amount
                    amount = 0
                    filename = ""
                    self._stage(150, downloaded=downloaded)
            if proc.wait():
                raise GpuError(self.install_error)
        self._update(downloaded=downloaded + amount, transfer=None)

    @contextmanager
    def _volume_lock(self):
        import fcntl

        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "install.lock").open("ab") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise GpuError(120) from error
            yield

    def _install(self, target, gpu=None):
        try:
            self._event(146)
            with self._volume_lock():
                self._prepare(target, gpu)
        except (OSError, subprocess.SubprocessError, GpuError) as error:
            print(f"GPU setup: {error}", file=sys.__stdout__, flush=True)
            reason = error.message_id if isinstance(error, GpuError) else self.install_error
            self._event(reason)
            self._update(state="error", error=reason)
        else:
            self._event(151)
            self._update(state="ready")

    def _uninstall(self, target, gpu, notify):
        try:
            notify(234)
            with self._volume_lock():
                if not self.installed(target, gpu):
                    raise GpuError(237)
                runtime = self.environment(target)
                marker = self._marker(target, gpu)
                shared = gpu and any(item != marker and item.is_file() for item in runtime.glob("gpu-name-GPU-*"))
                if shared:
                    marker.unlink()
                else:
                    shutil.rmtree(runtime)
        except (OSError, GpuError) as error:
            reason = error.message_id if isinstance(error, GpuError) else 236
            notify(reason)
            self._update(state="error", error=reason)
        else:
            notify(238 if shared else 235)
            self._update(state="idle")

    def _prepare(self, target, gpu=None):
        if self.installed(target, gpu):
            return
        runtime = self.environment(target)
        packages_ready = runtime / "packages-ready"
        python = str(runtime / "bin/python")
        if not self.packages_ready(target):
            if runtime.exists():
                shutil.rmtree(runtime)
            try:
                subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True)
                self._pip(python, "--index-url", PYPI_INDEX, "pip==26.2.2")
                self._pip(python, *self.requirements(target))
                packages_ready.touch()
            finally:
                if not packages_ready.is_file():
                    shutil.rmtree(runtime, ignore_errors=True)

        self._stage(111, state="checking")
        env = dict(os.environ, PYTHONNOUSERSITE="1")
        env.pop("HSA_OVERRIDE_GFX_VERSION", None)
        if gpu:
            env["CUDA_VISIBLE_DEVICES"] = gpu
        try:
            result = subprocess.run(
                [python, self.probe_script, target], env=env, check=True,
                stdout=subprocess.PIPE, text=True, timeout=90,
            )
            name = json.loads(result.stdout.strip().splitlines()[-1])
            if not isinstance(name, str) or not name.strip():
                raise ValueError("GPU name missing from probe")
        except (subprocess.SubprocessError, ValueError, IndexError) as error:
            raise GpuError(self.probe_error) from error
        marker = self._marker(target, gpu)
        pending = marker.with_suffix(".pending")
        pending.write_text(name, encoding="utf-8")
        pending.replace(marker)
