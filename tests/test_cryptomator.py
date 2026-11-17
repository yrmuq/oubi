import csv
import os
import sys
import tempfile
import threading
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oubi_cryptomator import COOKIE_NAME, Vault, VaultError
from oubi_files import VAULT_METADATA_ROOT, image_revision, sidecar_path
from oubi_index import folder_sidecars
from oubi_web import App, Handler, Workspace, images_under, read_favorites, read_info, recent_tagged, sort_command, source_folders, tags_in_folders
from sort_images import main as sort_main


class VaultWorkspaceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_default_vault_replaces_the_container_environment_setting(self):
        with patch("oubi_cryptomator.VAULT_PATH", self.root):
            self.assertIsNone(Vault().path)
            self.assertEqual(Vault().mount, Path("/mnt/oubi-crypt/mount"))
            (self.root / "vault.cryptomator").touch()
            self.assertEqual(Vault().path, self.root)
            with patch.object(Path, "is_file", side_effect=PermissionError):
                self.assertIsNone(Vault().path)

    def test_previous_vault_paths_work_for_gallery_sorting_and_deletion(self):
        album = self.root / "Album"
        album.mkdir()
        image = album / "example.jpg"
        image.touch()
        stored = str(VAULT_METADATA_ROOT / "Album" / image.name)
        with patch("oubi_files.VAULT_MOUNT", self.root), patch.dict(os.environ, {"OUBI_CRYPT_OUTPUT": "Sorted"}):
            workspace = Workspace.encrypted(self.root)
            with open(workspace.csv, "w", encoding="utf-8", newline="") as handle:
                csv.writer(handle).writerows([
                    ("file", "rating", "character", "copyright", "general", "style"),
                    (stored, "rating:g", "", "original", "scenery", ""),
                ])
            app = App(workspace)
            self.addCleanup(app.close_catalog)
            self.assertEqual(app.gallery(["Album"]), {"files": ["Album/example.jpg"]})
            self.assertEqual(app.gallery(["Album"], ["scenery"]), {"files": ["Album/example.jpg"]})
            self.assertIn("scenery", tags_in_folders(["Album"], path=workspace.csv, root=self.root))

            command = sort_command(folders=[str(album)], workspace=workspace)
            with patch.object(sys, "argv", command[1:]):
                sort_main()
            self.assertEqual(len(list(Path(workspace.output).rglob("*.jpg"))), 1)
            self.assertEqual(Path(workspace.sorted_list).read_text().splitlines(), [stored])

            app._count_folders(threading.Event(), "moved")
            self.assertEqual(app.folders()[0]["sorted"], 1)
            app.remove_image("Album/example.jpg", image_revision(image))
            self.assertNotIn(stored, Path(workspace.csv).read_text())
            self.assertEqual(Path(workspace.sorted_list).read_text(), "")

    def test_unlock_reports_fuse_error_and_redacts_password_in_response(self):
        (self.root / "vault.cryptomator").touch()
        vault = Vault(self.root, self.root / "mount")
        password = "fixture-password"

        def fail_to_mount(command, **options):
            self.assertNotIn(password, command)
            output = options["stdout"]
            output.write(b"fusermount3: mount failed: Operation not permitted (fixture-password)\n")
            output.write(b"Caused by: org.example.MountException: fuse_mount failed\n")
            output.flush()
            return SimpleNamespace(poll=lambda: 1, stdin=BytesIO())

        handler = Handler.__new__(Handler)
        handler.headers = Mock()
        handler.headers.get_content_type.return_value = "application/json"
        handler.app = SimpleNamespace(vault=vault)
        handler._language = Mock(return_value="en")
        handler._send = Mock()
        with patch("oubi_cryptomator.subprocess.Popen", side_effect=fail_to_mount):
            handler._vault_action("/api/vault/unlock", {"password": password})
        code, payload = handler._send.call_args.args
        self.assertEqual(code, 409)
        self.assertIn("Cryptomator exited with code 1 before mounting", payload["error"])
        self.assertIn("fusermount3: mount failed: Operation not permitted", payload["error"])
        self.assertNotIn(password, payload["error"])
        self.assertIsNone(vault.process)

    def test_unlock_reports_a_nonempty_mount_directory(self):
        (self.root / "vault.cryptomator").touch()
        mount = self.root / "mount"
        mount.mkdir()
        (mount / "leftover").touch()
        with self.assertRaises(VaultError) as failure:
            Vault(self.root, mount).unlock("fixture")
        self.assertEqual(failure.exception.message_id, 245)
        self.assertEqual(failure.exception.values["path"], mount)

    def test_unlock_distinguishes_data_permissions_from_a_fuse_failure(self):
        mount = self.root / "mount"
        vault = SimpleNamespace(lock=threading.Lock(), mount=mount, unlock=Mock(), close=Mock())
        handler = Handler.__new__(Handler)
        handler.headers = Mock()
        handler.headers.get_content_type.return_value = "application/json"
        handler.app = SimpleNamespace(vault=vault)
        handler._language = Mock(return_value="en")
        handler._send = Mock()
        with patch.object(Workspace, "encrypted", side_effect=PermissionError("cannot create .oubi")):
            handler._vault_action("/api/vault/unlock", {"password": "fixture"})
        code, payload = handler._send.call_args.args
        self.assertEqual(code, 409)
        self.assertIn("The vault mounted, but Oubi could not prepare its data directory", payload["error"])
        self.assertIn("cannot create .oubi", payload["error"])
        vault.close.assert_called_once()

    def test_favorites_use_all_csv_characters_and_stay_in_the_vault(self):
        workspace = Workspace.encrypted(self.root)
        image = self.root / "panel.jpg"
        image.touch()
        with open(workspace.csv, "w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows([("file", "character", "general"),
                                          (str(image), "first_character, second_character", "halloween")])
        app = App(workspace)
        self.assertEqual(app.toggle_favorite("panel.jpg", "second_character"),
                         {"character": ["second_character"], "event": []})
        self.assertEqual(app.toggle_favorite("panel.jpg", "halloween"),
                         {"character": ["second_character"], "event": ["halloween"]})
        reopened = App(workspace)
        self.assertEqual(reopened.favorites_path, self.root / ".oubi" / "favorites.json")
        self.assertEqual(read_favorites(reopened.favorites_path)["event"], ["halloween"])
        self.assertEqual(reopened.toggle_favorite("panel.jpg", "SECOND_CHARACTER"),
                         {"character": [], "event": ["halloween"]})
        with self.assertRaises(ValueError):
            reopened.toggle_favorite("../outside.jpg", "outside")

    def test_thumbnails_are_reused_replaced_and_removed_with_sources(self):
        workspace = Workspace.encrypted(self.root)
        images = self.root / "Album"
        images.mkdir()
        first, second = images / "a.jpg", images / "b.png"
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        app = App(workspace)
        handler = Handler.__new__(Handler)
        lock = threading.Lock()
        handler.app = SimpleNamespace(vault=SimpleNamespace(lock=lock))
        handler.headers = {}
        handler._scoped_app = Mock(return_value=app)
        cache_dir = self.root / ".oubi" / "thumbnails"

        with patch("oubi_web.render_thumbnail", side_effect=lambda data: b"preview of " + data) as render:
            def preview(path):
                return handler._thumbnail_response(path.relative_to(self.root).as_posix(), "scope")

            result = (200, b"preview of first", "image/webp")
            self.assertEqual(preview(first), result)
            self.assertEqual(preview(first), result)
            render.assert_called_once_with(b"first")
            first.write_bytes(b"updated source")
            self.assertEqual(preview(first)[1], b"preview of updated source")
            self.assertEqual(len(list(cache_dir.rglob("*.webp"))), 1)
            self.assertEqual(preview(second)[1], b"preview of second")

        app.remove_image(first.relative_to(self.root).as_posix(), image_revision(first))
        self.assertEqual([entry.read_bytes() for entry in cache_dir.rglob("*.webp")], [b"preview of second"])
        second.unlink()
        app._count_folders(threading.Event(), "recount")
        self.assertEqual(list(cache_dir.rglob("*.webp")), [])
        first.write_bytes(b"concurrent deletion")

        def remove_during_render(_):
            self.assertFalse(lock.locked())
            app.remove_image("Album/a.jpg", image_revision(first))
            return b"expired preview"

        with patch("oubi_web.render_thumbnail", side_effect=remove_during_render):
            self.assertEqual(handler._thumbnail_response("Album/a.jpg", "scope")[0], 404)
        self.assertEqual(list(cache_dir.rglob("*.webp")), [])
        app.close_catalog()

    def test_vault_scan_lists_sidecars_once_per_image_folder(self):
        workspace = Workspace.encrypted(self.root)
        folder = self.root / "Album"
        folder.mkdir()
        for filename in ("a.jpg", "a.png", "b.jpg"):
            (folder / filename).touch()
        sidecar = sidecar_path(folder / "a.png")
        sidecar.parent.mkdir()
        sidecar.write_text("moon", encoding="utf-8")
        app = App(workspace)
        with patch("oubi_web.folder_sidecars", wraps=folder_sidecars) as list_sidecars:
            app._count_folders(threading.Event(), "scan")
        list_sidecars.assert_called_once_with(folder)
        self.assertEqual(app.folders()[0]["total"], 3)
        self.assertEqual(app.gallery(["Album"], ["moon"]), {"files": ["Album/a.png"]})
        app.close_catalog()

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
            self.assertEqual(app.gallery(["Album"]), {"scanning": True})
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

    def test_vault_reuses_saved_index_and_tracks_deletion_until_manual_recount(self):
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

        with (patch("oubi_web.source_folders", wraps=source_folders) as scan,
              patch("oubi_web.recent_tagged", side_effect=AssertionError("Gallery rescanned the vault"))):
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
            path = tagged.relative_to(self.root).as_posix()
            self.assertEqual(app.gallery(["Album"]), {"files": [path]})
            self.assertEqual(app.gallery(["Album/Selected", "Album/Selected/Deep"]), {"files": [path]})
            self.assertEqual(app.gallery(["Album/Select"]), {"files": []})
            reopened = App(workspace)
            self.addCleanup(reopened.close_catalog)
            self.assertEqual(reopened.folders("Album/Selected"), [leaf])
            self.assertEqual(reopened.gallery(["Album"], ["MOON"]), {"files": [path]})
            self.assertEqual(reopened.catalog.vocabulary(["Album"]), ["moon"])
            many = ["Album", *(f"unused-{number}" for number in range(101))]
            self.assertEqual(reopened.gallery(many), {"files": [path]})
            self.assertEqual(reopened.gallery(many, ["moon"]), {"files": [path]})
            self.assertEqual(scan.call_count, 1)

            app.remove_image(path, image_revision(tagged))
            self.assertEqual(app.folders("Album/Selected")[0]["total"], 0)
            self.assertEqual(app.gallery(["Album"]), {"files": []})
            self.assertEqual(app.gallery(["Album"], ["moon"]), {"files": []})
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(app.status()["scan_progress"], completed)
            after_delete = App(workspace)
            self.addCleanup(after_delete.close_catalog)
            self.assertEqual(after_delete.folders("Album/Selected")[0]["total"], 0)
            self.assertEqual(after_delete.catalog.vocabulary(["Album"]), [])
            Path(workspace.csv).write_text(f"file\n{untagged.relative_to(self.root).as_posix()}\n", encoding="utf-8")
            self.assertTrue(app._ensure_count(refresh=True))
            refreshed = finish_scan()
            self.assertEqual(refreshed["total"], 1)
            self.assertNotEqual(refreshed["id"], completed["id"])
            self.assertEqual(app.folders("Album/Selected")[0]["scan"], "none")
            self.assertEqual(app.gallery(["Album"]), {"files": [untagged.relative_to(self.root).as_posix()]})
            self.assertEqual(scan.call_count, 2)

    def test_vault_index_updates_tag_jobs_and_recovers_dirty_snapshots(self):
        workspace = Workspace.encrypted(self.root)
        image = self.root / "a.jpg"
        image.touch()
        app = App(workspace)
        app._count_folders(threading.Event(), "initial")
        initial = app.catalog
        command = [sys.executable, "-c", """
import csv, json, os, sys
assert os.environ['OUBI_INDEX_EVENTS'] == '1'
print('OUBI_INDEX_CHANGES ' + json.dumps([sys.argv[1]]), flush=True)
with open(sys.argv[2], 'w') as out:
    csv.writer(out).writerows([('file', 'character', 'general'), (sys.argv[1], 'person', 'sun')])
""", str(image), workspace.csv]
        with patch("oubi_web.source_folders", side_effect=AssertionError("Job triggered a rescan")):
            app._run("tag", [str(self.root)], "en", command)
            self.assertIs(app.catalog, initial)
            self.assertEqual(app.folders()[0]["tagged"], 1)
            self.assertEqual(app.gallery([""], ["sun", "person"]), {"files": ["a.jpg"]})
        reopened = App(workspace)
        self.assertEqual(reopened.catalog.details("a.jpg")["character"], "person")
        reopened.catalog.mark_dirty()
        self.assertIsNone(App(workspace).catalog)
        reopened.close_catalog()
        app.close_catalog()

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
        app._count_folders(threading.Event(), "initial")
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
        self.assertEqual(app.folders()[0]["sorted"], 1)
        with patch.dict(os.environ, {"OUBI_COPY_TXT": "1", "OUBI_MOVE": "1"}):
            app._run("sort", selected, "en", None, output=workspace.output)
        self.assertEqual(app.folders()[0]["total"], 0)
        self.assertEqual(app.gallery([""]), {"files": []})

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
