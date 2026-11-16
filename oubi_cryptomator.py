"""Mount a configured Cryptomator vault for the browser that unlocked it."""

import os
import secrets
import signal
import subprocess
import tempfile
import threading
import time
from pathlib import Path

FUSE_MOUNTER = "org.cryptomator.frontend.fuse.mount.LinuxFuseMountProvider"
COOKIE_NAME = "oubi_vault"


class VaultError(Exception):
    def __init__(self, message_id):
        self.message_id = message_id
        super().__init__(message_id)


class Vault:
    def __init__(self, path=None, mount="/run/oubi-crypt/mount"):
        configured = path if path is not None else os.environ.get("OUBI_CRYPT")
        self.path = Path(configured) if configured else None
        self.mount = Path(mount)
        self.lock = threading.RLock()
        self.process = None
        self.token = ""
        self.scope = ""
        self.app = None

    def owns(self, token):
        return bool(self.token and token.isascii() and secrets.compare_digest(self.token, token)
                    and self.process is not None and self.process.poll() is None and self.is_mounted())

    def is_mounted(self):
        mount = str(self.mount).replace(chr(92), r"\134").replace(" ", r"\040")
        try:
            with open("/proc/self/mountinfo", encoding="utf-8") as mounts:
                return any(line.split()[4] == mount for line in mounts)
        except OSError:
            return self.mount.is_mount()

    def _unmount(self):
        process = self.process
        if process and process.poll() is None:
            try:
                process.send_signal(signal.SIGINT)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
        if self.is_mounted():
            try:
                subprocess.run(["fusermount3", "-u", str(self.mount)], timeout=5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if self.is_mounted():
            raise VaultError(170)
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
        if not (self.path / "vault.cryptomator").is_file():
            raise VaultError(166)
        try:
            self.mount.mkdir(mode=0o700, exist_ok=True)
            if any(self.mount.iterdir()):
                raise VaultError(169)
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
                expires = time.monotonic() + 20
                while self.process.poll() is None and time.monotonic() < expires:
                    if self.is_mounted():
                        self.token = secrets.token_urlsafe(32)
                        self.scope = secrets.token_hex(16)
                        return
                    time.sleep(0.05)
                output.seek(0)
                error_log = output.read(65536).lower()
                wrong_password = b"invalidpassphrase" in error_log or b"invalid passphrase" in error_log
                raise VaultError(164 if wrong_password else 169)
        except OSError as error:
            self.close()
            raise VaultError(169) from error
        except VaultError:
            self.close()
            raise

    def close(self):
        if self.process is not None:
            self._unmount()
        self.token = self.scope = ""
        self.app = self.process = None
