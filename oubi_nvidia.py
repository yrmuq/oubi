"""Select a PyTorch CUDA wheel set for a GPU visible to NVIDIA Container Toolkit."""

import csv
import json
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from oubi_gpu import GpuError, GpuRuntime, PYPI_INDEX, REQUIREMENTS, gpu_smoke_test

TORCH_VERSION = "2.14.0"
VISION_VERSION = "0.29.0"
GPU_UUID = re.compile(r"GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.IGNORECASE)
VERSION = re.compile(r"\d+(?:\.\d+)+")
# Minimum Linux driver and lowest compiled minor compute capability per GPU generation.
CUDA_BUILDS = {
    "cu130": ((580, 65, 6), {7: 5, 8: 0, 9: 0, 10: 0, 12: 0}),
    "cu126": ((525, 60, 13), {5: 0, 6: 0, 7: 0, 8: 0, 9: 0}),
}


def cuda_build(capability, driver):
    major, minor = map(int, capability.split("."))
    version = tuple(map(int, driver.split(".")))
    for name, (minimum, capabilities) in CUDA_BUILDS.items():
        if version >= minimum and major in capabilities and minor >= capabilities[major]:
            return name
    return ""


def detect_gpus():
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=uuid,name,compute_cap,driver_version", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, check=True, timeout=4,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    gpus = []
    for row in csv.reader(result.stdout.splitlines(), skipinitialspace=True):
        if len(row) != 4:
            continue
        uuid, name, capability, driver = map(str.strip, row)
        if not GPU_UUID.fullmatch(uuid) or not re.fullmatch(r"\d+\.\d+", capability) or not VERSION.fullmatch(driver):
            continue
        gpus.append({"uuid": uuid, "name": name, "capability": capability, "driver": driver,
                     "build": cuda_build(capability, driver)})
    return gpus


def nvidia_device(torch):
    if torch.version.cuda and not torch.version.hip and torch.cuda.is_available():
        return 0
    raise GpuError(131)


class NvidiaRuntime(GpuRuntime):
    missing_runtime = 132
    install_error = 133
    probe_error = 131
    probe_script = str(Path(__file__).resolve())
    # Approximate compressed wheels from the CUDA and PyPI indexes for these pins.
    download_bytes = {"cu126": 3_720_000_000, "cu130": 3_080_000_000}

    def __init__(self, root="/data/nvidia"):
        super().__init__(root)
        self.inventory_lock = threading.Lock()
        self.inventory_time = float("-inf")
        self.inventory = []

    def gpus(self, *, refresh=False):
        with self.inventory_lock:
            if refresh or time.monotonic() - self.inventory_time >= 5:
                self.inventory = detect_gpus()
                self.inventory_time = time.monotonic()
            return self.inventory

    def resolve(self, choice):
        if not isinstance(choice, str) or choice != "auto" and not GPU_UUID.fullmatch(choice):
            raise GpuError(129)
        gpus = self.gpus(refresh=True)
        if not gpus:
            raise GpuError(130)
        if choice == "auto":
            gpu = next((gpu for gpu in gpus if gpu["build"]), gpus[0])
        else:
            gpu = next((gpu for gpu in gpus if gpu["uuid"] == choice), None)
            if gpu is None:
                raise GpuError(130)
        if not gpu["build"]:
            raise GpuError(134)
        return gpu["build"], gpu["uuid"]

    def environment(self, build):
        return self.root / f"{build}-torch{TORCH_VERSION}-{sys.implementation.cache_tag}"

    def requirements(self, build):
        return (
            "--index-url", f"https://download.pytorch.org/whl/{build}",
            "--extra-index-url", PYPI_INDEX,
            f"torch=={TORCH_VERSION}+{build}", f"torchvision=={VISION_VERSION}+{build}",
            "-r", REQUIREMENTS,
        )

    def status(self):
        gpus = self.gpus()
        installed = {}
        for gpu in gpus:
            if gpu["build"] and (name := self.installed(gpu["build"], gpu["uuid"])):
                installed[gpu["uuid"]] = name
        return {
            "gpus": gpus,
            "default": next((gpu["uuid"] for gpu in gpus if gpu["build"]), gpus[0]["uuid"] if gpus else ""),
            "installed": installed,
            "install": self.installation_status(),
        }


if __name__ == "__main__":
    import torch

    print(json.dumps(gpu_smoke_test(torch, nvidia_device(torch), 131)))
