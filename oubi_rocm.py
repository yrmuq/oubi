"""Detect AMD GPUs and prepare an optional ROCm environment for the tagger."""

import json
import os
import sys
from pathlib import Path

from oubi_gpu import GpuError as RocmError
from oubi_gpu import GpuRuntime, PYPI_INDEX, REQUIREMENTS, gpu_smoke_test

AMD_INDEX = "https://stable.repo.amd.com/rocm/whl-next/"
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
# Published compressed wheel sizes for these pins, plus estimated PyPI dependencies.
DOWNLOAD_BYTES = {
    "gfx90a": 2_010_000_000, "gfx942": 3_400_000_000, "gfx950": 2_260_000_000,
    "gfx1100": 1_980_000_000, "gfx1101": 1_970_000_000, "gfx1102": 1_970_000_000,
    "gfx1103": 1_800_000_000, "gfx1150": 1_820_000_000, "gfx1151": 1_840_000_000,
    "gfx1152": 1_820_000_000, "gfx1200": 1_840_000_000, "gfx1201": 1_780_000_000,
}
TOPOLOGY = Path("/sys/class/kfd/kfd/topology/nodes")
DEVICES = Path("/dev")


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
    return gpu_smoke_test(torch, amd_device(torch, architecture), 117)


class RocmRuntime(GpuRuntime):
    missing_runtime = 118
    install_error = 121
    probe_error = 117
    probe_script = str(Path(__file__).resolve())
    download_bytes = DOWNLOAD_BYTES

    def __init__(self, root="/data/rocm", topology=TOPOLOGY, devices=DEVICES):
        super().__init__(root)
        self.topology = topology
        self.devices = devices
        configured = os.environ.get("OUBI_ROCM_ARCH", "auto").strip().lower()
        self.default = configured if configured in ARCHITECTURES else "auto"

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

    def status(self):
        visible = self.has_devices()
        gpus = read_gpu_topology(self.topology, self.devices) if visible else {}
        candidates = (gpus or ARCHITECTURES) if visible else ()
        installed = {arch: name for arch in candidates if (name := self.installed(arch))}
        return {
            "architectures": ARCHITECTURES,
            "default": self.default,
            "download_bytes": DOWNLOAD_BYTES,
            "visible": visible,
            "gpus": gpus,
            "installed": installed,
            "install": self.installation_status(),
        }

    def requirements(self, arch):
        return (
            "--index-url", AMD_INDEX, "--extra-index-url", PYPI_INDEX,
            f"torch[device-{arch}]=={TORCH_VERSION}+rocm{ROCM_VERSION}",
            f"torchvision[device-{arch}]=={VISION_VERSION}+rocm{ROCM_VERSION}",
            f"rocm[libraries,device-{arch}] @ {ROCM_METADATA}",
            "-r", REQUIREMENTS,
        )


if __name__ == "__main__":
    print(json.dumps(probe_gpu(sys.argv[1])))
