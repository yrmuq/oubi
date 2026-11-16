import csv
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oubi_cryptomator import COOKIE_NAME, VaultError
from oubi_files import image_revision, sidecar_path
from oubi_web import App, Handler, Workspace, images_under, read_info, recent_tagged, sort_command, source_folders, tags_in_folders


class VaultWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_vault_response_releases_lock_before_writing_to_a_disconnected_client(self):
        lock = threading.Lock()
        workspace = Workspace.encrypted(self.root)
        (self.root / "preview.jpg").touch()
        vault = SimpleNamespace(lock=lock, scope="test-scope", app=SimpleNamespace(paths=workspace), owns=lambda _: True)

        def read_image(_):
            self.assertTrue(lock.locked())
            return b"preview"

        def disconnect(*_):
            self.assertFalse(lock.locked())
            raise BrokenPipeError()

        for during_headers in (True, False):
            with self.subTest(during_headers=during_headers), patch.object(Path, "read_bytes", read_image):
                handler = Handler.__new__(Handler)
                handler.app = SimpleNamespace(vault=vault)
                handler.path = "/api/file?scope=test-scope&path=preview.jpg"
                handler.headers = {"Cookie": f"{COOKIE_NAME}=test-token"}
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock(side_effect=disconnect if during_headers else None)
                handler.wfile = Mock()
                handler.wfile.write.side_effect = disconnect
                handler.close_connection = False

                handler.do_GET()

                handler.send_response.assert_called_once_with(200)
                self.assertTrue(handler.close_connection)
                if during_headers:
                    handler.wfile.write.assert_not_called()
                else:
                    handler.wfile.write.assert_called_once_with(b"preview")

    def test_vault_preview_reuses_its_index_until_the_csv_is_replaced(self):
        workspace = Workspace.encrypted(self.root)
        image = self.root / "Album" / " photo.jpg"
        image.parent.mkdir()
        image.touch()
        index = Path(workspace.csv)
        index.write_text(f"file,character\n{image},alice\n", encoding="utf-8")
        app = App(workspace)
        handler = Handler.__new__(Handler)
        query = {"path": ["Album/ photo.jpg"]}
        open_file = Path.open
        reads = []

        def track_open(path, mode="r", *args, **kwargs):
            if path == index and mode == "r":
                reads.append(path)
            return open_file(path, mode, *args, **kwargs)

        with patch.object(Path, "open", track_open), patch("oubi_web.path_keys") as resolve_index_paths:
            for _ in range(2):
                status, info = handler._file_response("/api/tags", query, app)
                self.assertEqual((status, info["character"]), (200, "alice"))
            self.assertEqual(reads, [index])

            replacement = index.with_suffix(".new")
            replacement.write_text(f"file,character\n{image},carol\n", encoding="utf-8")
            original = index.stat()
            os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
            replacement.replace(index)
            self.assertEqual(handler._file_response("/api/tags", query, app)[1]["character"], "carol")
            self.assertEqual(reads, [index, index])

            self.assertEqual(handler._file_response("/api/tags", query, App(workspace))[1]["character"], "carol")
            self.assertEqual(reads, [index, index, index])
            resolve_index_paths.assert_not_called()

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

    def test_vault_reuses_root_scan_after_deletion_until_manual_recount(self):
        workspace = Workspace.encrypted(self.root)
        tagged = self.root / "Album" / "Selected" / "Deep" / "tagged.jpg"
        untagged = self.root / "Album" / "Selected" / "untagged.jpg"
        excluded = Path(workspace.output) / "excluded.jpg"
        for image in (tagged, untagged, excluded):
            image.parent.mkdir(parents=True, exist_ok=True)
            image.touch()
        tags = sidecar_path(tagged)
        tags.parent.mkdir()
        tags.write_text("moon")
        Path(workspace.sorted_list).write_text(f"{tagged}\n")
        app = App(workspace)
        self.addCleanup(app.stop_folder_count)

        def finish_scan():
            with app.lock:
                running = app.folder_scan
            if running:
                running[1].join(timeout=2)
            self.assertIsNone(app.folder_scan)
            return app.status()["scan_progress"]

        with patch("oubi_web.source_folders", wraps=source_folders) as scan:
            app.folders("Album")
            completed = finish_scan()
            self.assertEqual((completed["stage"], completed["done"], completed["total"]), ("done", 2, 2))
            for directory, child in (("", "Album"), ("Album", "Album/Selected")):
                with self.subTest(directory=directory):
                    folder, = app.folders(directory)
                    self.assertEqual((folder["path"], folder["total"], folder["scan"], folder["sort"]),
                                     (child, 2, "part", "part"))
            leaf, = app.folders("Album/Selected")
            self.assertEqual((leaf["path"], leaf["total"], leaf["scan"], leaf["sort"]),
                             ("Album/Selected/Deep", 1, "done", "done"))
            self.assertEqual(app.folders("Album/Selected/Deep"), [leaf])
            self.assertEqual(scan.call_count, 1)

            app.remove_image(str(tagged.relative_to(self.root)), image_revision(tagged))
            self.assertEqual(app.folders("Album/Selected"), [leaf])
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(app.status()["scan_progress"], completed)
            self.assertTrue(app._ensure_count(refresh=True))
            refreshed = finish_scan()
            self.assertEqual(refreshed["total"], 1)
            self.assertNotEqual(refreshed["id"], completed["id"])
            self.assertEqual(app.folders("Album/Selected")[0]["scan"], "none")
            self.assertEqual(scan.call_count, 2)

    def test_work_preview_uses_completed_tag_events(self):
        app = App(Workspace.encrypted(self.root))
        image = self.root / "Album" / " last.jpg"
        app.note(f"Now: tagging {image} (1 of 2).")
        self.assertEqual(app.status()["preview_file"], "")
        app.note(f'OUBI_TAG_PREVIEW "{image}"')
        app.note("Now: tagging batch 2–3 of 3.")
        self.assertEqual(app.status()["preview_file"], "Album/ last.jpg")
        self.assertNotIn("OUBI_TAG_PREVIEW", "\n".join(app.status()["log"]))

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
