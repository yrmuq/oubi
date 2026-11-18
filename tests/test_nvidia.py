import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from oubi_gpu import GpuError
from oubi_nvidia import NvidiaRuntime, cuda_build, detect_gpus, nvidia_device
from oubi_web import App
from tag_folder import main

FIRST = "GPU-11111111-1111-1111-1111-111111111111"
SECOND = "GPU-22222222-2222-2222-2222-222222222222"


class NvidiaTests(unittest.TestCase):
    def test_driver_and_architecture_choose_cuda_build(self):
        self.assertEqual(cuda_build("12.0", "580.126.20"), "cu130")
        self.assertEqual(cuda_build("12.0", "570.124.06"), "")
        self.assertEqual(cuda_build("8.9", "570.124.06"), "cu126")
        self.assertEqual(cuda_build("6.1", "590.44.01"), "cu126")
        self.assertEqual(cuda_build("8.6", "525.60.13"), "cu126")
        self.assertEqual(cuda_build("8.6", "525.60.12"), "")
        self.assertEqual(cuda_build("3.5", "590.44.01"), "")

    def inventory(self, modes=("cu130", "cu130")):
        return [
            {"uuid": uuid, "name": f"GPU {index + 1}", "build": build}
            for index, (uuid, build) in enumerate(zip((FIRST, SECOND), modes))
        ]

    def test_detection_parses_devices_and_normalizes_driver_availability(self):
        listing = f'{FIRST}, "Vendor, GPU", 8.9, 570.124.06\n{SECOND}, GPU 2, 12.0, 570.124.06\ninvalid, Extra, 8.0, 580.65.06'
        with patch("oubi_nvidia.subprocess.run", return_value=SimpleNamespace(stdout=listing)):
            items = detect_gpus()
        self.assertEqual([(item["name"], item["build"]) for item in items], [("Vendor, GPU", "cu126"), ("GPU 2", "")])

    def test_installs_bundle_once_and_probes_each_selected_gpu(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = NvidiaRuntime(directory)
            path = runtime.environment("cu130")

            def run(command, **kwargs):
                if command[1:3] == ["-m", "venv"]:
                    (path / "bin").mkdir(parents=True)
                    (path / "bin/python").touch()
                    return
                self.assertEqual(command[-1], "cu130")
                uuid = kwargs["env"]["CUDA_VISIBLE_DEVICES"]
                return SimpleNamespace(stdout=json.dumps(f"GPU {uuid}") + "\n")

            with patch("oubi_gpu.subprocess.run", side_effect=run) as runner:
                with patch.object(runtime, "_pip") as install:
                    runtime._install("cu130", FIRST)
                    runtime._install("cu130", SECOND)
                self.assertEqual(install.call_count, 2)
                self.assertIn("torch==2.14.0+cu130", install.call_args.args)
            self.assertEqual(runner.call_count, 3)
            self.assertEqual(runtime.installed("cu130", FIRST), f"GPU {FIRST}")
            self.assertEqual(runtime.installed("cu130", SECOND), f"GPU {SECOND}")

    def test_jobs_use_verified_selected_gpu_and_reject_invalid_requests(self):
        app = App()
        with patch.object(app.nvidia, "gpus", return_value=self.inventory(("", "cu130"))):
            self.assertEqual(app.nvidia.resolve("auto"), ("cu130", SECOND))
            with self.assertRaisesRegex(GpuError, "134"):
                app.nvidia.resolve(FIRST)
            with self.assertRaisesRegex(GpuError, "129"):
                app.nvidia.resolve("../untrusted")
            with patch("oubi_web.absolute_folders", return_value=(["/images"], None)):
                with patch("oubi_web.preflight", return_value=None):
                    error = app.start("tag", [""], device="nvidia", gpu=SECOND, language="en")
                    self.assertIn("Install and check NVIDIA support", error)
                    with patch.object(app.nvidia, "python_for", return_value="/data/nvidia/bin/python"):
                        with patch("oubi_web.threading.Thread") as thread:
                            self.assertIsNone(app.start("tag", [""], device="nvidia", gpu=SECOND, language="en"))
                    command = thread.call_args.kwargs["args"][-1]
                    self.assertEqual(command[0], "/data/nvidia/bin/python")
                    self.assertEqual(command[-4:], ["--device", "nvidia", "--gpu-uuid", SECOND])

    def test_tagger_selects_uuid_before_loading_torch(self):
        def load(*args):
            self.assertEqual(os.environ["CUDA_VISIBLE_DEVICES"], SECOND)
            self.assertEqual(args[1], "nvidia")
            raise GpuError(131)

        with patch.dict(os.environ, {}, clear=False):
            with patch("sys.argv", ["tag_folder.py", "/images", "--device", "nvidia", "--gpu-uuid", SECOND]):
                with patch("tag_folder.load_tagger", side_effect=load):
                    self.assertEqual(main(), 1)
        torch = SimpleNamespace(version=SimpleNamespace(hip=None, cuda="13.0"), cuda=SimpleNamespace(is_available=lambda: True))
        self.assertEqual(nvidia_device(torch), 0)
        torch.version.hip = "AMD"
        with self.assertRaisesRegex(GpuError, "131"):
            nvidia_device(torch)
