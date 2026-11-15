"""Detect AMD GPUs and prepare an optional ROCm environment for the tagger."""

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

AMD_INDEX = "https://stable.repo.amd.com/rocm/whl-next/"
PYPI_INDEX = "https://pypi.org/simple"
ROCM_VERSION = "10.1.0"
TORCH_VERSION = "2.14.0"
VISION_VERSION = "0.29.0a0"
ROCM_METADATA = (
    f"{AMD_INDEX}rocm/rocm-{ROCM_VERSION}.tar.gz"
    "#sha256=e6616e62ebd1324031681e6e59349d46ee4f143a8d8f4f901a91c35c993041fc"
)
ARCHITECTURES = (
    "gfx90a", "gfx942", "gfx950", "gfx1100", "gfx1101",
    "gfx1102", "gfx1103", "gfx1150", "gfx1151", "gfx1152", "gfx1200", "gfx1201",
)
# Compressed AMD artifacts for the pinned release, excluding regular Python dependencies.
DOWNLOAD_BYTES = {"gfx1102": 1_901_695_635}
TOPOLOGY = Path("/sys/class/kfd/kfd/topology/nodes")
DEVICES = Path("/dev")
PIP_PROGRESS = re.compile(r"Progress (\d+) of (\d+)")


class RocmError(Exception):
    def __init__(self, message_id):
        self.message_id = message_id
        super().__init__(message_id)


def read_gpu_topology(topology=TOPOLOGY, devices=DEVICES):
    gpus = {}
    for path in sorted(topology.glob("*/properties")):
        try:
            properties = dict(line.split(maxsplit=1) for line in path.read_text().splitlines() if line)
            render_minor = int(properties["drm_render_minor"])
            version = int(properties["gfx_target_version"])
        except (OSError, ValueError, KeyError):
            continue
        if version <= 0 or not os.access(devices / "dri" / f"renderD{render_minor}", os.R_OK | os.W_OK):
            continue
        major, rest = divmod(version, 10000)
        minor, stepping = divmod(rest, 100)
        arch = f"gfx{major}{minor:x}{stepping:x}"
        try:
            name = path.with_name("name").read_text().strip()
        except OSError:
            name = ""
        gpus.setdefault(arch, name or arch)
    return gpus


def amd_device(torch, architecture):
    if torch.version.hip and torch.cuda.is_available():
        for index in range(torch.cuda.device_count()):
            properties = torch.cuda.get_device_properties(index)
            device_arch = getattr(properties, "gcnArchName", "").split(":", 1)[0]
            if device_arch == architecture:
                return index
    raise RocmError(117)


def probe_gpu(architecture):
    import torch
    from transformers import pipeline

    if not callable(pipeline):
        raise RocmError(117)

    index = amd_device(torch, architecture)
    device = f"cuda:{index}"
    matrix = torch.ones((32, 32), device=device, dtype=torch.float16)
    actual = (matrix @ matrix)[0, 0].item()
    if actual != 32:
        raise RocmError(117)
    return torch.cuda.get_device_name(index)


class RocmRuntime:
    def __init__(self, root="/data/rocm", topology=TOPOLOGY, devices=DEVICES):
        self.root = Path(root)
        self.topology = topology
        self.devices = devices
        configured = os.environ.get("OUBI_ROCM_ARCH", "auto").strip().lower()
        self.default = configured if configured in ARCHITECTURES else "auto"
        self.lock = threading.Lock()
        self.install = {"state": "idle"}

    def has_devices(self):
        return (
            os.access(self.devices / "kfd", os.R_OK | os.W_OK)
            and any(os.access(path, os.R_OK | os.W_OK) for path in (self.devices / "dri").glob("renderD*"))
        )

    def resolve(self, choice):
        if not isinstance(choice, str) or choice != "auto" and choice not in ARCHITECTURES:
            raise RocmError(112)
        if not self.has_devices():
            raise RocmError(113)
        gpus = read_gpu_topology(self.topology, self.devices)
        if choice == "auto":
            for arch in gpus:
                if arch in ARCHITECTURES:
                    return arch
            raise RocmError(114 if not gpus else 115)
        if gpus and choice not in gpus:
            raise RocmError(116)
        return choice

    def environment(self, arch):
        return self.root / f"rocm{ROCM_VERSION}-torch{TORCH_VERSION}-{sys.implementation.cache_tag}-{arch}"

    def installed(self, arch):
        runtime = self.environment(arch)
        try:
            if not (runtime / "bin/python").is_file():
                return ""
            return (runtime / "gpu-name").read_text().strip()
        except OSError:
            return ""

    def python_for(self, arch):
        if not self.installed(arch):
            raise RocmError(118)
        return str(self.environment(arch) / "bin/python")

    def status(self):
        visible = self.has_devices()
        gpus = read_gpu_topology(self.topology, self.devices) if visible else {}
        candidates = (gpus or ARCHITECTURES) if visible else ()
        installed = {arch: name for arch in candidates if (name := self.installed(arch))}
        with self.lock:
            install = dict(self.install)
        return {
            "architectures": ARCHITECTURES,
            "default": self.default,
            "download_bytes": DOWNLOAD_BYTES,
            "visible": visible,
            "gpus": gpus,
            "installed": installed,
            "install": install,
        }

    def start(self, choice):
        arch = self.resolve(choice)
        if platform.machine() != "x86_64" or sys.version_info[:2] != (3, 12):
            raise RocmError(119)
        with self.lock:
            if self.install["state"] in {"installing", "checking"}:
                raise RocmError(120)
            if self.installed(arch):
                return
            self.install = {"state": "installing", "arch": arch}
        threading.Thread(target=self._install, args=(arch,), daemon=True).start()

    def _update(self, **values):
        with self.lock:
            self.install.update(values)

    def _pip(self, python, *args):
        command = [
            python, "-m", "pip", "--isolated", "--disable-pip-version-check", "--no-input",
            "--no-color", "install", "--only-binary=:all:", "--no-cache-dir", *args,
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
                raise RocmError(121)

    def _install(self, arch):
        import fcntl

        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / "install.lock").open("ab") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as error:
                    raise RocmError(120) from error
                self._prepare(arch)
        except (OSError, subprocess.SubprocessError, RocmError) as error:
            print(f"ROCm setup: {error}", flush=True)
            self._update(state="error", error=error.message_id if isinstance(error, RocmError) else 121)
        else:
            self._update(state="ready")

    def _prepare(self, arch):
        if self.installed(arch):
            return
        runtime = self.environment(arch)
        if runtime.exists():
            shutil.rmtree(runtime)
        try:
            subprocess.run([sys.executable, "-m", "venv", str(runtime)], check=True)
            python = str(runtime / "bin/python")
            self._pip(python, "--progress-bar", "raw", "--index-url", PYPI_INDEX, "pip==26.2.2")
            self._pip(
                python, "--progress-bar", "raw", "--index-url", AMD_INDEX, "--extra-index-url", PYPI_INDEX,
                f"torch[device-{arch}]=={TORCH_VERSION}+rocm{ROCM_VERSION}",
                f"torchvision[device-{arch}]=={VISION_VERSION}+rocm{ROCM_VERSION}",
                f"rocm[libraries,device-{arch}] @ {ROCM_METADATA}",
                "-r", str(Path(__file__).with_name("requirements.txt")),
            )
            self._update(state="checking", received=0, total=0)
            env = dict(os.environ, PYTHONNOUSERSITE="1")
            env.pop("HSA_OVERRIDE_GFX_VERSION", None)
            try:
                result = subprocess.run(
                    [python, str(Path(__file__).resolve()), arch], env=env, check=True,
                    stdout=subprocess.PIPE, text=True, timeout=90,
                )
                name = json.loads(result.stdout.strip().splitlines()[-1])
            except (subprocess.SubprocessError, ValueError, IndexError) as error:
                raise RocmError(117) from error
            marker = runtime / "gpu-name.pending"
            marker.write_text(name, encoding="utf-8")
            marker.replace(runtime / "gpu-name")
        finally:
            if not self.installed(arch):
                shutil.rmtree(runtime, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(probe_gpu(sys.argv[1])))
