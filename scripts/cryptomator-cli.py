"""Fetch the locked Cryptomator CLI or update its lock from a signed release."""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import Request, urlopen

PROJECT = "cryptomator/cli"
RELEASES = f"https://github.com/{PROJECT}/releases/download"
# Published in Cryptomator's installer-signature documentation.
SIGNER = "58117AFA1F85B3EEC154677D615D449FE6E6A235"
VERSION = re.compile(r"v?(\d+\.\d+\.\d+)")


def asset_url(tag):
    match = VERSION.fullmatch(tag)
    if not match:
        raise ValueError(f"Invalid Cryptomator release tag: {tag}")
    return f"{RELEASES}/{tag}/cryptomator-cli-{match[1]}-linux-x64.zip"


def fetch(url, destination):
    with urlopen(url, timeout=60) as source, destination.open("wb") as output:
        shutil.copyfileobj(source, output)


def sha256(file):
    with file.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def update(lock, current):
    request = Request(f"https://api.github.com/repos/{PROJECT}/releases/latest")
    if token := os.environ.get("GH_TOKEN"):
        request.add_header("Authorization", f"Bearer {token}")
    with urlopen(request, timeout=30) as response:
        release = json.load(response)
    tag = release["tag_name"]
    url = asset_url(tag)
    previous = tuple(map(int, VERSION.fullmatch(current["version"])[1].split(".")))
    available = tuple(map(int, VERSION.fullmatch(tag)[1].split(".")))
    if available <= previous:
        return
    urls = {asset["browser_download_url"] for asset in release["assets"]}
    if not {url, url + ".asc"} <= urls:
        raise ValueError("Cryptomator release has no signed Linux x64 archive")
    with TemporaryDirectory() as temporary:
        directory = Path(temporary)
        bundle = directory / "cli.zip"
        signature = directory / "cli.zip.asc"
        key = directory / "cryptomator.asc"
        keyring = directory / "cryptomator.gpg"
        fetch(url, bundle)
        fetch(url + ".asc", signature)
        fetch("https://github.com/cryptobot.gpg", key)
        subprocess.run(["gpg", "--batch", "--dearmor", "--output", keyring, key], check=True)
        result = subprocess.run(["gpg", "--batch", "--no-autostart", "--homedir", temporary,
                                 "--no-default-keyring", "--keyring", keyring, "--status-fd", "1",
                                 "--verify", signature, bundle],
                                check=True, text=True, stdout=subprocess.PIPE)
        if not any(line.split()[-1] == SIGNER for line in result.stdout.splitlines()
                   if line.startswith("[GNUPG:] VALIDSIG ")):
            raise ValueError("Cryptomator release signature has an unexpected fingerprint")
        lock.write_text(json.dumps({"version": tag, "sha256": sha256(bundle)}, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("download", "update"))
    parser.add_argument("lock", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    args = parser.parse_args()
    current = json.loads(args.lock.read_text(encoding="utf-8"))
    if args.operation == "update":
        update(args.lock, current)
    else:
        if args.destination is None:
            parser.error("download requires a destination")
        fetch(asset_url(current["version"]), args.destination)
        if sha256(args.destination) != current["sha256"]:
            args.destination.unlink()
            raise ValueError("Cryptomator archive failed its SHA-256 check")


if __name__ == "__main__":
    main()
