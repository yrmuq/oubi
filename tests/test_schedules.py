import json
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oubi_schedule import ScheduleError, Schedules, cron_matches, parse_cron, vault_credential
from oubi_web import App, execute_schedule, release_schedule, reserve_schedule, write_json_file


class CronTests(unittest.TestCase):
    def test_range_list_step_and_sunday(self):
        rule = parse_cron("0,15-45/15 3 * * 7")
        self.assertTrue(cron_matches(rule, datetime(2026, 11, 22, 3, 15)))
        self.assertFalse(cron_matches(rule, datetime(2026, 11, 23, 3, 15)))
        self.assertFalse(cron_matches(rule, datetime(2026, 11, 22, 3, 20)))

    def test_two_restricted_day_fields_match_either(self):
        rule = parse_cron("0 3 25 11 0")
        self.assertTrue(cron_matches(rule, datetime(2026, 11, 25, 3)))
        self.assertTrue(cron_matches(rule, datetime(2026, 11, 22, 3)))
        self.assertFalse(cron_matches(rule, datetime(2026, 11, 21, 3)))

    def test_invalid_fields(self):
        for expression in ("@daily", "60 * * * *", "*/0 * * * *", "* * * * 8", "50-20 * * * *"):
            with self.subTest(expression=expression), self.assertRaises(ScheduleError):
                parse_cron(expression)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "schedules.json"
        self.reserve = Mock(return_value=True)
        self.execute = Mock(return_value=337)
        self.release = Mock()
        self.report = Mock()
        self.scheduler = self.make_scheduler()
        self.addCleanup(self.scheduler.close)

    def make_scheduler(self):
        return Schedules(self.path, write_json_file, self.reserve, self.execute, self.release, self.report)

    def run_slot(self, now):
        self.scheduler.queue_due(now)
        self.wait_for_worker()

    def wait_for_worker(self):
        with self.scheduler.lock:
            worker = self.scheduler.worker
        if worker:
            worker.join(timeout=2)
            self.assertFalse(worker.is_alive())

    def test_wait_then_run_once_and_persist_result(self):
        self.scheduler.add("* * * * *", "images", "photos/trip", "both", None)
        with self.assertRaises(ScheduleError):
            self.scheduler.add("* * * * *", "images", "../photos", "tag", None)
        now = datetime.now().astimezone()
        self.reserve.return_value = False
        self.run_slot(now)
        self.assertTrue(self.scheduler.public()[0]["pending"])
        self.execute.assert_not_called()
        self.reserve.return_value = True
        self.run_slot(now)
        self.run_slot(now)
        self.execute.assert_called_once()
        result = self.make_scheduler().public()[0]
        self.assertEqual(result["result"], 337)
        self.assertIsInstance(result["last_run"], int)

    def test_run_now_coalesces_clicks_and_keeps_the_cron_time(self):
        next_hour = (datetime.now().astimezone() + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        expression = f"0 {next_hour.hour} * * *"
        id_ = self.scheduler.add(expression, "images", "trip", "duplicates", None)

        self.reserve.return_value = False
        for _ in range(2):
            self.scheduler.run_now(id_)
            self.wait_for_worker()
        self.assertTrue(self.scheduler.public()[0]["pending"])
        self.execute.assert_not_called()

        self.reserve.return_value = True
        self.execute.side_effect = lambda *args: self.scheduler.run_now(id_) or 337
        self.scheduler.run_now(id_)
        self.wait_for_worker()
        self.execute.assert_called_once()
        result = self.make_scheduler().public()[0]
        self.assertEqual(result["cron"], expression)
        self.assertEqual(result["result"], 337)
        self.assertFalse(result["pending"])
        self.assertNotIn("slot", json.loads(self.path.read_text())[id_])

        self.run_slot(next_hour)
        self.run_slot(next_hour)
        self.assertEqual(self.execute.call_count, 2)

    def test_credentials_are_private_and_password_replacement_unblocks(self):
        def credential(path, id_, operation, value):
            if operation == "decrypt":
                raise ScheduleError(328)
            return "sealed-secret"

        with patch("oubi_schedule.vault_credential", side_effect=credential):
            id_ = self.scheduler.add("* * * * *", "vault", "", "duplicates", "my secret")
            saved = self.path.read_text()
            self.assertNotIn("my secret", saved)
            self.assertIn("sealed-secret", saved)
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("credential", self.scheduler.public()[0])
            self.run_slot(datetime.now().astimezone())
            self.assertTrue(self.scheduler.public()[0]["blocked"])
            self.execute.assert_not_called()
            self.scheduler.replace_password(id_, "new secret")
            self.assertFalse(self.scheduler.public()[0]["blocked"])
            self.scheduler.reset(id_)
        self.assertEqual(json.loads(self.path.read_text()), {})

    def test_reset_during_run_stays_reset(self):
        id_ = self.scheduler.add("* * * * *", "images", "", "tag", None)
        self.execute.side_effect = lambda rule, password, stopping: self.scheduler.reset(id_) or 337
        self.run_slot(datetime.now().astimezone())
        self.release.assert_called_once()
        self.assertEqual(self.scheduler.public(), [])
        self.assertEqual(self.make_scheduler().public(), [])

    @patch("oubi_schedule.local_tpm_available", return_value=True)
    @patch("oubi_schedule.subprocess.run")
    def test_tpm_secret_subprocess_uses_stdin_and_both_keys(self, run, available):
        run.return_value = SimpleNamespace(stdout=b"sealed")
        directory = self.path.parent
        self.assertEqual(vault_credential(directory, "rule", "encrypt", "my password"), "sealed")
        command = run.call_args.args[0]
        self.assertIn("--with-key=host+tpm2", command)
        self.assertIn("--tpm2-pcrs=7", command)
        self.assertIn("--tpm2-device=/dev/tpmrm0", command)
        self.assertNotIn("my password", command)
        self.assertEqual(run.call_args.kwargs["input"], b"my password")
        self.assertEqual(run.call_args.kwargs["env"]["SYSTEMD_CREDENTIAL_SECRET"],
                         str(directory / ".oubi-tpm" / "credential.secret"))
        run.side_effect = subprocess.CalledProcessError(1, command, stderr=b"private diagnostic")
        with self.assertRaises(ScheduleError) as error:
            vault_credential(directory, "rule", "decrypt", "sealed")
        self.assertEqual(error.exception.code, 328)

    @patch("oubi_schedule.request_tpm", return_value="oubi-tpm-v1:sealed")
    @patch("oubi_schedule.tpm_service_configured", return_value=True)
    @patch("oubi_schedule.local_tpm_available", return_value=False)
    def test_routes_credentials_by_format_and_reports_migration(self, direct, service, request):
        directory = self.path.parent
        encrypted = vault_credential(directory, "rule", "encrypt", "password")
        request.assert_called_once_with("encrypt", "rule", "password")
        request.return_value = "password"
        self.assertEqual(vault_credential(directory, "rule", "decrypt", encrypted), "password")
        request.assert_called_with("decrypt", "rule", encrypted)
        with self.assertRaises(ScheduleError) as error:
            vault_credential(directory, "rule", "decrypt", "original-systemd-credential")
        self.assertEqual(error.exception.code, 366)


class ScheduledWorkspaceTests(unittest.TestCase):
    def test_vault_waits_and_background_open_cannot_supply_a_browser_scope(self):
        app = App()
        app.model_state = "ready"
        rule = {"source": "vault", "folder": "trip", "action": "both"}
        app.vault.process = object()
        self.assertFalse(reserve_schedule(app, rule))
        app.vault.process = None
        app.vault.path = Path("/crypt")
        self.assertTrue(reserve_schedule(app, rule))
        with (patch("oubi_web.Vault") as vault, patch("oubi_web.Workspace.encrypted"),
              patch("oubi_web.App") as background, patch("oubi_web.run_scheduled_operations", return_value=337)):
            background.return_value.duplicate_store = None
            result = execute_schedule(app, rule, "password", threading.Event())
            self.assertEqual(result, 337)
            vault.return_value.unlock.assert_called_once_with("password")
            vault.return_value.close.assert_called_once_with()
            self.assertIsNone(app.vault.app)
            self.assertEqual(app.vault.scope, "")
        release_schedule(app, rule)
        self.assertIsNone(app.schedule_scope)
