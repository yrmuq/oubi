"""Minute-based background jobs and TPM-bound Cryptomator credentials."""

import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from uuid import uuid4


TPM_DEVICE = "/dev/tpmrm0"
CRON_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
CRON_ITEM = re.compile(r"(\*|[0-9]+(?:-[0-9]+)?)(?:/([0-9]+))?", re.ASCII)
SCHEDULE_ACTIONS = ("tag", "duplicates", "both")


class ScheduleError(Exception):
    def __init__(self, code, **values):
        self.code = code
        self.values = values


def parse_cron(expression):
    fields = expression.split()
    if len(fields) != 5:
        raise ScheduleError(324)
    parsed = []
    for field, (low, high) in zip(fields, CRON_LIMITS):
        values = set()
        for item in field.split(","):
            match = CRON_ITEM.fullmatch(item)
            if match is None:
                raise ScheduleError(324)
            base, step = match.groups()
            if base == "*":
                start, end = low, high
            elif "-" in base:
                start, end = map(int, base.split("-"))
            elif step is None:
                start = end = int(base)
            else:
                raise ScheduleError(324)
            stride = int(step) if step is not None else 1
            if not low <= start <= end <= high or not 1 <= stride <= high - low + 1:
                raise ScheduleError(324)
            values.update(range(start, end + 1, stride))
        parsed.append((values, field.startswith("*")))
    return parsed


def cron_matches(fields, now):
    (minutes, _), (hours, _), (days, all_days), (months, _), (weekdays, all_weekdays) = fields
    weekday = (now.weekday() + 1) % 7
    day_matches = now.day in days
    weekday_matches = weekday in weekdays or (weekday == 0 and 7 in weekdays)
    calendar_matches = (day_matches and weekday_matches) if all_days or all_weekdays else (day_matches or weekday_matches)
    return now.minute in minutes and now.hour in hours and now.month in months and calendar_matches


def schedule_folder(value):
    if not isinstance(value, str) or len(value) > 4096 or "\0" in value:
        raise ScheduleError(325)
    relative = Path(value.strip())
    if relative.is_absolute() or any(part == ".." or part == "tags" or part.startswith(".") for part in relative.parts):
        raise ScheduleError(325)
    return relative.as_posix() if relative.parts else ""


def tpm_available():
    return os.access(TPM_DEVICE, os.R_OK | os.W_OK)


def vault_credential(directory, id_, operation, value):
    if not tpm_available():
        raise ScheduleError(326)
    secret_dir = directory / ".oubi-tpm"
    code = 327 if operation == "encrypt" else 328
    command = ["systemd-creds", f"--name=oubi-schedule-{id_}", f"--tpm2-device={TPM_DEVICE}"]
    if operation == "encrypt":
        command.extend(("--with-key=host+tpm2", "--tpm2-pcrs="))
    try:
        secret_dir.mkdir(mode=0o700, exist_ok=True)
        if secret_dir.is_symlink():
            raise OSError
        result = subprocess.run(
            [*command, operation, "-", "-"], input=value.encode("utf-8"), capture_output=True,
            env={**os.environ, "SYSTEMD_CREDENTIAL_SECRET": str(secret_dir / "credential.secret")},
            timeout=25, check=True,
        )
        encoded = result.stdout.decode("utf-8")
        if not encoded.strip():
            raise ValueError
        return encoded
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError) as error:
        raise ScheduleError(code) from error


class Schedules:
    def __init__(self, path, write_json, reserve, execute, release, report_error):
        self.path = path
        self.write_json = write_json
        self.reserve = reserve
        self.execute = execute
        self.release = release
        self.report_error = report_error
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.thread = None
        self.worker = None
        self.pending = {}
        self.load_failed = False
        try:
            self.rules = self._read_rules()
        except (OSError, ValueError, ScheduleError):
            self.load_failed = True
            self.rules = {}
            report_error(329)

    def _read_rules(self):
        try:
            if self.path.is_symlink():
                raise ValueError
            rules = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(rules, dict):
            raise ValueError
        for id_, rule in rules.items():
            if (re.fullmatch(r"[0-9a-f]{32}", id_) is None or not isinstance(rule, dict)
                    or not isinstance(rule.get("cron"), str) or len(rule["cron"]) > 128
                    or rule.get("source") not in ("images", "vault") or rule.get("action") not in SCHEDULE_ACTIONS
                    or (rule["source"] == "vault" and not isinstance(rule.get("credential"), str))):
                raise ValueError
            parse_cron(rule["cron"])
            schedule_folder(rule.get("folder"))
            if rule.get("result") == 333:
                rule["result"] = 336
        return rules

    def _save(self, rules):
        if self.load_failed:
            raise ScheduleError(329)
        try:
            self.write_json(self.path, rules)
        except OSError as error:
            raise ScheduleError(330) from error
        self.rules = rules

    def _encrypt(self, id_, password):
        if not isinstance(password, str) or not password or any(char in password for char in "\r\n\0"):
            raise ScheduleError(165)
        try:
            if len(password.encode("utf-8")) > 4096:
                raise ScheduleError(165)
        except UnicodeError as error:
            raise ScheduleError(165) from error
        return vault_credential(self.path.parent.resolve(), id_, "encrypt", password)

    def add(self, cron, source, folder, action, password):
        if not isinstance(cron, str) or len(cron) > 128:
            raise ScheduleError(324)
        cron = " ".join(cron.split())
        parse_cron(cron)
        folder = schedule_folder(folder)
        if source not in ("images", "vault") or action not in SCHEDULE_ACTIONS:
            raise ScheduleError(331)
        id_ = uuid4().hex
        rule = {"cron": cron, "source": source, "folder": folder, "action": action}
        if source == "vault":
            rule["credential"] = self._encrypt(id_, password)
        with self.lock:
            self._save({**self.rules, id_: rule})
        return id_

    def replace_password(self, id_, password):
        with self.lock:
            rule = self.rules.get(id_)
            if rule is None or rule["source"] != "vault":
                raise ScheduleError(332)
            if rule.get("result") == 333:
                raise ScheduleError(334)
        credential = self._encrypt(id_, password)
        with self.lock:
            rule = self.rules.get(id_)
            if rule is None:
                raise ScheduleError(332)
            if rule.get("result") == 333:
                raise ScheduleError(334)
            self._save({**self.rules, id_: {**rule, "credential": credential, "blocked": False}})

    def reset(self, id_):
        with self.lock:
            if id_ not in self.rules:
                raise ScheduleError(332)
            self._save({key: rule for key, rule in self.rules.items() if key != id_})
            self.pending.pop(id_, None)

    def public(self):
        with self.lock:
            if self.load_failed:
                raise ScheduleError(329)
            return [{"id": id_, "pending": id_ in self.pending,
                     **{key: rule.get(key) for key in ("cron", "source", "folder", "action", "last_run", "result", "blocked")}}
                    for id_, rule in self.rules.items()]

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True, name="oubi-schedules")
        self.thread.start()

    def close(self):
        self.stopping.set()
        if self.thread is not None:
            self.thread.join()
        with self.lock:
            worker = self.worker
        if worker is not None:
            worker.join()

    def _loop(self):
        while not self.stopping.is_set():
            try:
                self.queue_due(datetime.now().astimezone())
            except ScheduleError as error:
                self.report_error(error.code)
                self.stopping.wait(60)
            else:
                self.stopping.wait(1)

    def queue_due(self, now):
        slot = int(now.timestamp()) // 60
        with self.lock:
            for id_, rule in self.rules.items():
                if (not rule.get("blocked") and rule.get("result") != 333 and slot > rule.get("slot", -1)
                        and cron_matches(parse_cron(rule["cron"]), now)):
                    self.pending.setdefault(id_, slot)
            if self.pending and self.worker is None:
                self.worker = threading.Thread(target=self.run_pending, args=(slot,), daemon=True)
                self.worker.start()

    def run_pending(self, slot):
        try:
            self._process_pending(slot)
        except ScheduleError as error:
            self.report_error(error.code)
            self.stopping.wait(30)
        finally:
            with self.lock:
                self.worker = None

    def _process_pending(self, slot):
        with self.lock:
            due = list(self.pending)
        for id_ in due:
            if self.stopping.is_set():
                return
            with self.lock:
                rule = self.rules.get(id_)
                if rule is None or not self.reserve(rule):
                    continue
                try:
                    running = {**rule, "slot": max(slot, int(time.time()) // 60),
                               "last_run": int(time.time()), "result": 333}
                    self._save({**self.rules, id_: running})
                except ScheduleError:
                    self.release(rule)
                    raise
                self.pending.pop(id_, None)
            try:
                password = (vault_credential(self.path.parent.resolve(), id_, "decrypt", running["credential"])
                            if running["source"] == "vault" else None)
                result = self.execute(running, password, self.stopping)
            except ScheduleError as error:
                result = error.code
            except Exception:
                result = 335
            finally:
                self.release(running)
            with self.lock:
                if self.rules.get(id_) is running:
                    finished = {**running, "result": result, "blocked": result in (164, 328)}
                    try:
                        self._save({**self.rules, id_: finished})
                    except ScheduleError:
                        self.rules[id_] = finished
                        raise
