import tempfile
import unittest
from unittest.mock import patch

from oubi_gpu import GpuError, GpuRuntime
from oubi_nvidia import NvidiaRuntime
from oubi_rocm import RocmRuntime
from oubi_web import App

FIRST = "GPU-11111111-1111-1111-1111-111111111111"
SECOND = "GPU-22222222-2222-2222-2222-222222222222"


class GpuInstallTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = App()
        self.app.rocm = RocmRuntime(temporary.name)
        self.app.nvidia = NvidiaRuntime(temporary.name)
        platform = patch.object(GpuRuntime, "check_platform")
        platform.start()
        self.addCleanup(platform.stop)

    def test_amd_confirmation_can_cancel_and_reject_a_stale_answer(self):
        app = self.app
        with patch.object(app.rocm, "resolve", return_value="gfx1102") as resolve:
            with patch.object(app.rocm, "launch_install") as launch:
                app.request_gpu_action("amd", "auto", "en")
                first = app.gpu_prompt["id"]
                self.assertIn("AMD ROCm gfx1102 needs a download of about 1.97 GB", app.status()["log"][-1])
                with self.assertRaisesRegex(GpuError, "152"):
                    app.request_gpu_action("nvidia", "auto", "en")
                with self.assertRaisesRegex(GpuError, "144"):
                    app.answer_gpu_prompt(first, "install", "en")
                app.answer_gpu_prompt(first, "n", "en")
                launch.assert_not_called()
                self.assertEqual(app.status()["log"][-2:], ["> n", "GPU installation canceled."])

                app.request_gpu_action("amd", "auto", "en")
                with self.assertRaisesRegex(GpuError, "145"):
                    app.answer_gpu_prompt(first, "y", "en")
                app.answer_gpu_prompt(app.gpu_prompt["id"], " Y ", "en")
                launch.assert_called_once()
                self.assertEqual(launch.call_args.args, ("gfx1102", None))
                resolve.assert_called_with("gfx1102")
                self.assertIsNone(app.gpu_prompt)

    def test_nvidia_auto_uses_confirmed_card_and_reuses_downloaded_packages(self):
        app = self.app
        with patch.object(app.nvidia, "gpus") as gpus:
            cards = [{"uuid": FIRST, "name": "First", "build": "cu126"},
                     {"uuid": SECOND, "name": "Second", "build": "cu130"}]
            gpus.return_value = cards
            app.request_gpu_action("nvidia", "auto", "en")
            self.assertIn("First (CUDA cu126)", app.status()["log"][-1])
            gpus.return_value = cards[::-1]
            with patch.object(app.nvidia, "launch_install") as launch:
                app.answer_gpu_prompt(app.gpu_prompt["id"], "y", "en")
            self.assertEqual(launch.call_args.args, ("cu126", FIRST))

            path = app.nvidia.environment("cu130")
            (path / "bin").mkdir(parents=True)
            (path / "bin/python").touch()
            (path / "packages-ready").touch()
            app.request_gpu_action("nvidia", SECOND, "en")
            self.assertIn("Second (CUDA cu130) is already downloaded (0 GB)", app.status()["log"][-1])
            cards[1]["build"] = "cu126"
            with patch.object(app.nvidia, "launch_install") as launch:
                with self.assertRaisesRegex(GpuError, "154"):
                    app.answer_gpu_prompt(app.gpu_prompt["id"], "y", "en")
                launch.assert_not_called()

    def install_fixture(self, runtime, target, gpu=None):
        environment = runtime.environment(target)
        (environment / "bin").mkdir(parents=True, exist_ok=True)
        (environment / "bin/python").touch()
        runtime._marker(target, gpu).write_text("Test GPU", encoding="utf-8")
        return environment

    def test_amd_removal_cancels_and_rechecks_usage_after_confirmation(self):
        app = self.app
        amd = app.rocm
        selected = self.install_fixture(amd, "gfx1102")
        other = self.install_fixture(amd, "gfx1100")
        with patch.object(amd, "resolve", return_value="gfx1102"):
            app.request_gpu_action("amd", "gfx1102", "en", action="uninstall")
            self.assertIn("Remove AMD ROCm gfx1102?", app.gpu_prompt["message"])
            app.answer_gpu_prompt(app.gpu_prompt["id"], "n", "en")
            self.assertTrue(selected.is_dir())
            self.assertIn("GPU removal canceled.", app.status()["log"])

            app.request_gpu_action("amd", "gfx1102", "en", action="uninstall")
            amd.acquire("gfx1102")
            try:
                with self.assertRaisesRegex(GpuError, "233"):
                    app.answer_gpu_prompt(app.gpu_prompt["id"], "y", "en")
                self.assertTrue(selected.is_dir())
            finally:
                amd.release("gfx1102")

            app.request_gpu_action("amd", "gfx1102", "en", action="uninstall")
            with patch("oubi_gpu.threading.Thread") as thread:
                app.answer_gpu_prompt(app.gpu_prompt["id"], "y", "en")
                with self.assertRaisesRegex(GpuError, "120"):
                    amd.acquire("gfx1102")
                worker = thread.call_args.kwargs
                worker["target"](*worker["args"])
        self.assertFalse(selected.exists())
        self.assertTrue(other.is_dir())
        self.assertIn("GPU components removed.", app.status()["log"])

    def test_cuda_removal_keeps_packages_until_the_last_card_is_removed(self):
        runtime = self.app.nvidia
        path = self.install_fixture(runtime, "cu126", FIRST)
        self.install_fixture(runtime, "cu126", SECOND)
        notices = []
        runtime._uninstall("cu126", FIRST, notices.append)
        self.assertFalse(runtime.installed("cu126", FIRST))
        self.assertTrue(runtime.installed("cu126", SECOND))
        self.assertIn(238, notices)
        runtime._uninstall("cu126", SECOND, notices.append)
        self.assertFalse(path.exists())
        self.assertIn(235, notices)

    def test_gpu_progress_reads_pip_bytes_without_changing_job_progress(self):
        app = self.app
        runtime = app.rocm
        app.percent = 35
        runtime._update(state="installing", expected=20_971_520, downloaded=1_048_576)
        with patch.object(app.nvidia, "gpus", return_value=[]):
            with patch("oubi_gpu.subprocess.Popen") as process:
                pip = process.return_value.__enter__.return_value
                pip.stdout = iter(["Downloading sample.whl (8.0 MB)", "Progress 4194304 of 8388608",
                                   "Progress 8388608 of 8388608", "Installing collected packages: sample"])
                pip.wait.return_value = 0
                with patch.object(runtime, "_event") as report:
                    with patch("oubi_gpu.time.monotonic", side_effect=[0, 0, 3, 4]):
                        runtime._pip("python", "sample")
                transfer = report.call_args_list[1].kwargs["transfer"]
                self.assertEqual(transfer["received"], 4_194_304)
                self.assertGreater(transfer["speed"], 0)
                self.assertEqual(runtime.installation_status()["downloaded"], 9_437_184)
                self.assertEqual(app.gpu_status("en")["gpu_progress"]["percent"], 45)
                self.assertEqual(app.status()["percent"], 35)
            runtime._stage(111, state="checking")
            self.assertEqual(app.gpu_status("en")["gpu_progress"]["percent"], 100)
            runtime._update(state="ready")
            self.assertIsNone(app.gpu_status("en")["gpu_progress"])
