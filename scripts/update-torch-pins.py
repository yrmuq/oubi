"""Update the Dockerfile with the CPU wheel pair selected by pip."""
import json
import re
import sys
from pathlib import Path


def update(dockerfile, report):
    packages = {"torch", "torchvision"}
    versions = {
        item["metadata"]["name"].lower(): item["metadata"]["version"]
        for item in report["install"]
        if item["metadata"]["name"].lower() in packages
    }
    if versions.keys() != packages or not all(re.fullmatch(r"\d+(?:\.\d+){2}\+cpu", value) for value in versions.values()):
        raise ValueError("Expected compatible stable CPU wheels for torch and torchvision")

    original = dockerfile.read_text(encoding="utf-8")
    updated = original
    for name, version in versions.items():
        pattern = rf"(?<![\w-]){name}==(\d+\.\d+\.\d+)\+cpu\b"
        match = re.search(pattern, original)
        if match is None:
            raise ValueError(f"Missing {name} pin in {dockerfile}")
        current = tuple(map(int, match[1].split(".")))
        available = tuple(map(int, version.removesuffix("+cpu").split(".")))
        if available < current:
            return
        updated, count = re.subn(pattern, f"{name}=={version}", updated)
        if count != 1:
            raise ValueError(f"Expected one {name} pin in {dockerfile}")
    if original != updated:
        dockerfile.write_text(updated, encoding="utf-8")


if __name__ == "__main__":
    with Path(sys.argv[1]).open(encoding="utf-8") as stream:
        update(Path("Dockerfile"), json.load(stream))
