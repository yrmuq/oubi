"""Resolve GPU wheel sets against the registries used by the installer."""
import hashlib
import json
import re
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import unquote
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oubi_gpu import DEPENDENCIES, GPU_WHEELS, PYPI_INDEX, REQUIREMENTS
from oubi_rocm import AMD_INDEX, ARCHITECTURES

WHEEL_LINE = re.compile(r"(torch|torchvision)==([0-9][0-9.a-z]*)\+([a-z0-9.]+)")
ROCM_TAG = re.compile(r"rocm-(\d+\.\d+\.\d+)\.tar\.gz")


def resolve(index, requirements, channel):
    shared = [line.strip() for line in Path(REQUIREMENTS).read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.lstrip().startswith("#")]
    with TemporaryDirectory() as temporary:
        project = Path(temporary) / "pyproject.toml"
        project.write_text(f"""[project]
name = "oubi-wheel-resolution"
version = "0"
requires-python = "==3.12.*"
dependencies = {json.dumps([*shared, *requirements])}

[tool.uv.sources]
torch = {{ index = "wheels" }}
torchvision = {{ index = "wheels" }}

[[tool.uv.index]]
name = "pypi"
url = {json.dumps(PYPI_INDEX)}

[[tool.uv.index]]
name = "wheels"
url = {json.dumps(index)}
""", encoding="utf-8")
        command = ["uv", "pip", "compile", str(project), "--directory", temporary,
                   "--constraint", str(DEPENDENCIES / "shared.txt"),
                   "--python-version", "3.12", "--python-platform", "x86_64-manylinux_2_36",
                   "--index-strategy", "unsafe-best-match",
                   "--only-binary=:all:", "--no-header", "--no-annotate", "--no-strip-extras"]
        result = subprocess.run(command, text=True, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if "does not have an extra" in result.stderr:
        raise ValueError("The candidate dropped a supported GPU architecture")
    wheels = {}
    for line in result.stdout.splitlines():
        match = WHEEL_LINE.fullmatch(re.sub(r"\[[^\]]+\]", "", line))
        if match and match[3] == channel:
            wheels[match[1]] = match[2]
    if wheels.keys() != {"torch", "torchvision"}:
        raise ValueError(f"Expected both wheel versions from {channel}")
    return wheels


def release_number(value):
    return tuple(map(int, value.split(".")))


class RocmReleases(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = set()

    def handle_starttag(self, tag, attributes):
        if tag == "a" and (match := ROCM_TAG.fullmatch(unquote(dict(attributes).get("href", "")))):
            self.tags.add(match[1])


def rocm(current):
    parser = RocmReleases()
    with urlopen(AMD_INDEX + "rocm/", timeout=30) as response:
        parser.feed(response.read().decode())
    extras = ",".join(f"device-{arch}" for arch in ARCHITECTURES)
    for tag in sorted(parser.tags, key=release_number, reverse=True):
        if release_number(tag) < release_number(current["rocm"]):
            continue
        channel = f"rocm{tag}"
        url = f"{AMD_INDEX}rocm/rocm-{tag}.tar.gz"
        with urlopen(url, timeout=30) as response:
            archive = response.read(1024 * 1024 + 1)
            if len(archive) > 1024 * 1024:
                raise ValueError("Unexpectedly large ROCm metadata archive")
        sha = hashlib.sha256(archive).hexdigest()
        if tag == current["rocm"] and sha != current["sha256"]:
            raise ValueError("The locked ROCm metadata archive changed upstream")
        # A direct URL fixes the ROCm series while the resolver checks every supported extra.
        requirement = f"rocm[libraries,{extras}] @ {url}#sha256={sha}"
        try:
            wheels = resolve(AMD_INDEX, [f"torch[{extras}]>={current['torch']}",
                                         f"torchvision[{extras}]>={current['torchvision']}", requirement], channel)
        except (ValueError, subprocess.CalledProcessError) as error:
            print(f"ROCm {tag} did not resolve for all supported devices: {getattr(error, 'stderr', '') or error}",
                  file=sys.stderr)
            continue
        return {"rocm": tag, **wheels, "sha256": sha}
    return current


def main():
    updated = {"amd": rocm(GPU_WHEELS["amd"]), "nvidia": {}}
    for channel, current in GPU_WHEELS["nvidia"].items():
        try:
            updated["nvidia"][channel] = resolve(f"https://download.pytorch.org/whl/{channel}",
                                                [f"{name}>={version}" for name, version in current.items()], channel)
        except (ValueError, subprocess.CalledProcessError) as error:
            print(f"{channel} did not resolve: {getattr(error, 'stderr', '') or error}", file=sys.stderr)
            updated["nvidia"][channel] = current
    if updated != GPU_WHEELS:
        (DEPENDENCIES / "gpu.json").write_text(json.dumps(updated, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
