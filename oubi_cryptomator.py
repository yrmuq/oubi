"""Mount the Cryptomator vault for the browser that unlocked it."""

import re
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from os import SEEK_END
from pathlib import Path

from oubi_files import VAULT_MOUNT

FUSE_MOUNTER = "org.cryptomator.frontend.fuse.mount.LinuxFuseMountProvider"
COOKIE_NAME = "oubi_vault"
VAULT_PATH = Path("/crypt")
VAULT_CONFIG = "vault.cryptomator"
MOUNT_TIMEOUT = 20


def default_vault_path():
    try:
        return VAULT_PATH if (VAULT_PATH / VAULT_CONFIG).is_file() else None
    except OSError:
        return None


class VaultError(Exception):
    def __init__(self, message_id, detail="", **values):
        self.message_id = message_id
        self.detail = detail
        self.values = values
        super().__init__(message_id)


def mount_diagnostic(log, password):
    lines = [line.strip() for line in log.replace(password, "[redacted]").splitlines() if line.strip()]
    fuse = next((line for line in reversed(lines)
                 if re.search(r"(?:^|\s)(?:fusermount\d*|fuse|libfuse):", line, re.IGNORECASE)), "")
    causes = [line.removeprefix("Caused by: ") for line in lines if line.startswith("Caused by: ")]
    errors = [line for line in lines if re.search(r"(?:Exception|Error):|\b(?:error|failed|denied|not permitted)\b",
                                                 line, re.IGNORECASE)]
    detail = fuse or (causes or errors or [""])[-1]
    return detail[:1000]


class Vault:
    def __init__(self, path=None, mount=VAULT_MOUNT):
        self.path = default_vault_path() if path is None else Path(path)
        self.mount = Path(mount)
        self.lock = threading.RLock()
        self.process = None
        self.token = ""
        self.scope = ""
        self.app = None

    def owns(self, token):
        owner, process = self.token, self.process
        return bool(owner and token.isascii() and secrets.compare_digest(owner, token)
                    and process is not None and process.poll() is None and self.is_mounted()
                    and owner == self.token and process is self.process)

    def is_mounted(self):
        mount = str(self.mount).replace(chr(92), r"\134").replace(" ", r"\040")
        try:
            with open("/proc/self/mountinfo", encoding="utf-8") as mounts:
                return any(line.split()[4] == mount for line in mounts)
        except OSError:
            return self.mount.is_mount()

    def _unmount(self):
        process = self.process
        detail = str(self.mount)
        if process and process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        if self.is_mounted():
            try:
                result = subprocess.run(["fusermount3", "-u", str(self.mount)], timeout=5,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace")
                detail = result.stderr.strip()[:1000] or detail
            except (OSError, subprocess.TimeoutExpired) as error:
                detail = str(error)
        if self.is_mounted():
            raise VaultError(170, detail)
        if process and process.poll() is None:
            process.kill()
            process.wait()

    def unlock(self, password):
        if not self.path:
            raise VaultError(166)
        if self.process is not None and self.process.poll() is None and self.is_mounted():
            raise VaultError(167)
        if not isinstance(password, str) or not password or any(char in password for char in "\r\n\0"):
            raise VaultError(165)
        try:
            encoded = password.encode("utf-8")
        except UnicodeError as error:
            raise VaultError(165) from error
        if len(encoded) > 4096:
            raise VaultError(165)
        self.close()
        config = self.path / VAULT_CONFIG
        try:
            if not config.is_file():
                raise VaultError(166, str(config))
        except OSError as error:
            raise VaultError(243, str(error)) from error
        try:
            self.mount.mkdir(mode=0o711, exist_ok=True)
            if any(self.mount.iterdir()):
                raise VaultError(245, path=self.mount)
        except OSError as error:
            raise VaultError(244, str(error), path=self.mount) from error

        try:
            command = [
                "cryptomator-cli", "unlock", "--password:stdin", "--mounter=" + FUSE_MOUNTER,
                "--mountPoint=" + str(self.mount), str(self.path),
            ]
            with tempfile.TemporaryFile() as output:
                self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=output,
                                                stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    with self.process.stdin as stdin:
                        stdin.write(encoded + b"\n")
                except BrokenPipeError:
                    pass
                expires = time.monotonic() + MOUNT_TIMEOUT
                while self.process.poll() is None and time.monotonic() < expires:
                    if self.is_mounted():
                        self.token = secrets.token_urlsafe(32)
                        self.scope = secrets.token_hex(16)
                        return
                    time.sleep(0.05)
                size = output.seek(0, SEEK_END)
                output.seek(max(0, size - 65536))
                log = output.read(65536).decode("utf-8", errors="replace")
                if re.search(r"invalid\s*passphrase", log, re.IGNORECASE):
                    raise VaultError(164)
                detail = mount_diagnostic(log, password)
                code = self.process.poll()
                if code is None:
                    raise VaultError(247, detail, path=self.mount, seconds=MOUNT_TIMEOUT)
                raise VaultError(248, detail, code=code, path=self.mount)
        except OSError as error:
            self.close()
            raise VaultError(246, str(error)) from error
        except VaultError:
            self.close()
            raise

    def close(self):
        if self.app is not None:
            self.app.stop_folder_count()
        if self.process is not None:
            self._unmount()
        if self.app is not None:
            self.app.close_catalog()
        self.token = self.scope = ""
        self.app = self.process = None
