"""Install and verify an optional GPU Python environment in the data volume."""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

PYPI_INDEX = "https://pypi.org/simple"
REQUIREMENTS = str(Path(__file__).with_name("requirements.txt"))
PIP_PROGRESS = re.compile(r"Progress (\d+) of (\d+)")


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

    def launch_install(self, target, gpu=None):
        if platform.system() != "Linux" or platform.machine() != "x86_64" or sys.version_info[:2] != (3, 12):
            raise GpuError(119)
        with self.lock:
            if self.install["state"] in {"installing", "checking"}:
                raise GpuError(120)
            if self.installed(target, gpu):
                return
            self.install = {"state": "installing", "arch": target, "gpu": gpu}
        threading.Thread(target=self._install, args=(target, gpu), daemon=True).start()

    def installation_status(self):
        with self.lock:
            return dict(self.install)

    def _update(self, **values):
        with self.lock:
            self.install.update(values)

    def _pip(self, python, *args):
        command = [
            python, "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input",
            "--no-color", "install", "--only-binary=:all:", "--no-cache-dir", "--progress-bar", "raw", *args,
        ]
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True) as proc:
            for line in proc.stdout:
                if match := PIP_PROGRESS.fullmatch(line.strip()):
                    amount, total = map(int, match.groups())
                    self._update(received=amount, total=total)
                else:
                    print(line.rstrip(), flush=True)
                    if line.lstrip().startswith(("Downloading ", "Installing collected packages:")):
                        self._update(received=0, total=0)
            if proc.wait():
                raise GpuError(self.install_error)

    def _install(self, target, gpu=None):
        import fcntl

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "install.lock").open("ab") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise GpuError(120) from error
                self._prepare(target, gpu)
        except (OSError, subprocess.SubprocessError, GpuError) as error:
            print(f"GPU setup: {error}", flush=True)
            self._update(state="error", error=error.message_id if isinstance(error, GpuError) else self.install_error)
        else:
            self._update(state="ready")

    def _prepare(self, target, gpu=None):
        if self.installed(target, gpu):
            return
        runtime = self.environment(target)
        packages_ready = runtime / "packages-ready"
        python = str(runtime / "bin/python")
        if not packages_ready.is_file() or not Path(python).is_file():
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

        self._update(state="checking", received=0, total=0)
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
