import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from oubi_rocm import RocmError, RocmRuntime, amd_device
from oubi_web import App, tag_command


class RocmTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.devices = self.root / "dev"
        self.topology = self.root / "topology"
        self.runtime = RocmRuntime(self.root / "rocm", self.topology, self.devices)

    def add_gpu(self, node, minor, target, *, mapped=True):
        properties = self.topology / str(node) / "properties"
        properties.parent.mkdir(parents=True)
        properties.write_text(f"drm_render_minor {minor}\ngfx_target_version {target}\n")
        properties.with_name("name").write_text("Radeon")
        if mapped:
            dri = self.devices / "dri"
            dri.mkdir(parents=True, exist_ok=True)
            (dri / f"renderD{minor}").touch()
            (self.devices / "kfd").touch()

    def test_architecture_selection_uses_mapped_devices(self):
        self.assertFalse(self.runtime.status()["visible"])
        with self.assertRaisesRegex(RocmError, "113"):
            self.runtime.resolve("auto")
        self.add_gpu(0, 128, 110002)
        self.add_gpu(1, 129, 110001, mapped=False)
        self.assertEqual(self.runtime.resolve("auto"), "gfx1102")
        self.assertEqual(self.runtime.status()["gpus"], {"gfx1102": "Radeon"})
        with self.assertRaisesRegex(RocmError, "116"):
            self.runtime.resolve("gfx1101")
        with self.assertRaisesRegex(RocmError, "112"):
            self.runtime.resolve("../other")
        self.add_gpu(2, 130, 90010)
        self.assertEqual(self.runtime.resolve("gfx90a"), "gfx90a")

    def test_manual_choice_works_without_topology_and_reads_compose_default(self):
        self.add_gpu(0, 128, 110002)
        (self.topology / "0" / "properties").unlink()
        with self.assertRaisesRegex(RocmError, "114"):
            self.runtime.resolve("auto")
        with patch.dict(os.environ, {"OUBI_ROCM_ARCH": "gfx1102"}):
            runtime = RocmRuntime(self.runtime.root, self.topology, self.devices)
        self.assertEqual(runtime.status()["default"], "gfx1102")
        self.assertEqual(runtime.resolve("gfx1102"), "gfx1102")

    def test_publishes_environment_after_successful_probe_only(self):
        arch = "gfx1102"
        environment = self.runtime.environment(arch)

        def run(command, **kwargs):
            if command[1:3] == ["-m", "venv"]:
                (environment / "bin").mkdir(parents=True)
                (environment / "bin/python").touch()
                return
            self.assertEqual(command[-1], arch)
            self.assertNotIn("HSA_OVERRIDE_GFX_VERSION", kwargs["env"])
            self.assertFalse(self.runtime.installed(arch))
            return SimpleNamespace(stdout=json.dumps("AMD Radeon RX 7600") + "\n")

        with patch("oubi_rocm.subprocess.run", side_effect=run):
            with patch.object(self.runtime, "_pip", side_effect=RocmError(121)):
                self.runtime._install(arch)
            self.assertEqual(self.runtime.install["state"], "error")
            self.assertFalse(environment.exists())
            with patch.object(self.runtime, "_pip") as install:
                self.runtime._install(arch)
            self.assertIn("torch[device-gfx1102]==2.14.0+rocm10.1.0", install.call_args.args)
        self.assertEqual(self.runtime.install["state"], "ready")
        self.assertEqual(self.runtime.installed(arch), "AMD Radeon RX 7600")
        self.assertEqual(self.runtime.python_for(arch), str(environment / "bin/python"))

    def test_gpu_job_selects_runtime_and_never_falls_back_to_cpu(self):
        self.add_gpu(0, 128, 110002)
        app = App()
        app.rocm = self.runtime
        with patch("oubi_web.absolute_folders", return_value=(["/images"], None)):
            with patch("oubi_web.preflight", return_value=None):
                error = app.start("tag", [""], device="amd", architecture="gfx1102", language="en")
                self.assertIn("Install and check AMD support", error)
                self.assertFalse(app.job_running)
                with patch.object(self.runtime, "python_for", return_value="/data/rocm/bin/python"):
                    with patch("oubi_web.threading.Thread") as thread:
                        self.assertIsNone(app.start("tag", [""], device="amd", language="en"))
                command = thread.call_args.kwargs["args"][-1]
                self.assertEqual(command[0], "/data/rocm/bin/python")
                self.assertEqual(command[-4:], ["--device", "amd", "--gpu-arch", "gfx1102"])
                self.assertIn("cpu", tag_command(["/images"]))

    def test_pytorch_picks_matching_amd_device(self):
        cuda = SimpleNamespace(
            is_available=lambda: True,
            device_count=lambda: 2,
            get_device_properties=lambda index: SimpleNamespace(gcnArchName=("gfx1100", "gfx1102:sramecc-")[index]),
        )
        torch = SimpleNamespace(version=SimpleNamespace(hip="HIP"), cuda=cuda)
        self.assertEqual(amd_device(torch, "gfx1102"), 1)
        with self.assertRaisesRegex(RocmError, "117"):
            amd_device(torch, "gfx1101")
        torch.version.hip = None
        with self.assertRaisesRegex(RocmError, "117"):
            amd_device(torch, "gfx1102")
