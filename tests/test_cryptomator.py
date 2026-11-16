import csv
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from oubi_cryptomator import VaultError
from oubi_files import sidecar_path
from oubi_web import App, Workspace, images_under, read_info, recent_tagged, sort_command, source_folders, tags_in_folders


class VaultWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_vault_destination_stays_within_mount_and_is_excluded_from_source(self):
        with patch.dict(os.environ, {"OUBI_CRYPT_OUTPUT": "Gallery/Sorted"}):
            workspace = Workspace.encrypted(self.root)
        source = self.root / "Gallery"
        output = Path(workspace.output)
        output.mkdir(parents=True)
        (output / "sorted.jpg").touch()
        original = source / "keep.jpg"
        original.touch()
        self.assertEqual(images_under(source, workspace.excluded), [original])
        self.assertEqual(Path(workspace.csv).parent, self.root / ".oubi")
        for invalid in (".", "../external", "/tmp/external", ".oubi/archive", "Gallery/tags"):
            with self.subTest(path=invalid), patch.dict(os.environ, {"OUBI_CRYPT_OUTPUT": invalid}):
                with self.assertRaisesRegex(VaultError, "168"):
                    Workspace.encrypted(self.root)

    def test_vault_folders_appear_while_counting_runs_and_can_be_cancelled(self):
        (self.root / "Album").mkdir()
        app = App(Workspace.encrypted(self.root))
        started = threading.Event()
        release = threading.Event()
        self.addCleanup(release.set)
        self.addCleanup(app.stop_folder_count)

        def slow_count(*args, cancelled, **kwargs):
            started.set()
            while not cancelled() and not release.wait(0.01):
                continue
            return []

        with patch("oubi_web.source_folders", side_effect=slow_count):
            self.assertEqual([folder["path"] for folder in app.folders()], ["Album"])
            self.assertTrue(started.wait(1))
            app.stop_folder_count()
        self.assertIsNone(app.folder_scan)
        self.assertIsNone(app.folder_cache)

    def test_folder_scan_reports_discovery_then_exact_checked_counts(self):
        first = self.root / "a.jpg"
        second = self.root / "b.png"
        first.touch()
        second.touch()
        sidecar_path(first).parent.mkdir()
        sidecar_path(first).write_text("moon", encoding="utf-8")
        updates = []

        def progress(stage, done, total=None):
            updates.append((stage, done, total))

        folders = source_folders(self.root, csv=self.root / "missing.csv", sorted_list=self.root / "missing.txt",
                                 progress=progress)
        self.assertEqual([(folder["total"], folder["tagged"]) for folder in folders], [(2, 1)])
        self.assertEqual(updates, [
            ("discover", 1, None), ("discover", 2, None), ("checking", 0, 2),
            ("checking", 1, 2), ("checking", 2, 2), ("done", 2, 2),
        ])

    def test_folder_navigation_lists_children_without_exposing_vault_output(self):
        (self.root / "Album" / "Selected").mkdir(parents=True)
        (self.root / "Album" / "Sorted").mkdir()
        (self.root / "Album" / "tags").mkdir()
        with patch.dict(os.environ, {"OUBI_CRYPT_OUTPUT": "Album/Sorted"}):
            app = App(Workspace.encrypted(self.root))
        self.addCleanup(app.stop_folder_count)
        self.assertEqual([item["path"] for item in app.folders("Album")], ["Album/Selected"])
        self.assertEqual([item["path"] for item in app.folders("Album/Selected")], ["Album/Selected"])
        for path in ("Album/Sorted", "Album/tags", ".oubi", "../outside"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                app.folders(path)

    def test_sort_plan_and_sort_share_destination_and_private_index(self):
        with patch.dict(os.environ, {"OUBI_CRYPT_OUTPUT": "Sorted"}):
            workspace = Workspace.encrypted(self.root)
        original = self.root / "src.jpg"
        original.write_bytes(b"image")
        with open(workspace.csv, "w", newline="") as handle:
            fields = ["file", "rating", "character", "copyright", "general", "style"]
            writer = csv.writer(handle)
            writer.writerow(fields)
            writer.writerow([str(original), "rating:g", "person", "series", "", ""])
        tags = sidecar_path(original)
        tags.parent.mkdir()
        tags.write_text("rating:g, person")
        app = App(workspace)
        selected = [str(self.root)]
        self.assertIn("--dry-run", sort_command(True, selected, workspace=workspace))
        with patch.dict(os.environ, {"OUBI_COPY_TXT": "1", "OUBI_MOVE": "0"}):
            app._run("plan", selected, "en", None, output=workspace.output)
            self.assertFalse(Path(workspace.output).exists())
            self.assertIn(workspace.output, "\n".join(app.status()["log"]))
            app._run("sort", selected, "en", None, output=workspace.output)
        result, = Path(workspace.output).rglob("*.jpg")
        self.assertEqual(sidecar_path(result).read_text(), "rating:g, person")
        self.assertEqual(Path(workspace.sorted_list).read_text().strip(), str(original))

    def test_web_reads_only_tags_subfolder_and_keeps_image_extensions_distinct(self):
        first = self.root / "a.jpg"
        second = self.root / "a.png"
        for path in (first, second):
            path.touch()
        (self.root / "a.txt").write_text("legacy")
        self.assertEqual(recent_tagged([""], root=self.root), [])
        sidecar_path(first).parent.mkdir()
        sidecar_path(first).write_text("moon")
        sidecar_path(second).write_text("sun")
        self.assertEqual(read_info("a.jpg", root=self.root, csv=self.root / "missing")["text"], "moon")
        self.assertEqual(read_info("a.png", root=self.root, csv=self.root / "missing")["text"], "sun")
        self.assertEqual(tags_in_folders([""], path=self.root / "missing", root=self.root), ["moon", "sun"])
