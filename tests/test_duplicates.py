import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from oubi_cryptomator import Vault
from oubi_duplicates import DuplicateError, DuplicateStore, duplicate_file, matching_pairs
from oubi_files import image_revision, sidecar_path
from oubi_index import VaultIndex
from oubi_web import App, Handler, Workspace


class DuplicateTests(unittest.TestCase):
    def test_band_search_returns_direct_matches_only(self):
        entries = [("a", "", 0), ("b", "", 255), ("c", "", 511), ("d", "", 0)]
        self.assertEqual(
            set(matching_pairs(entries, lambda: False)),
            {("a", "b", 8), ("b", "c", 1), ("a", "d", 0), ("b", "d", 8)},
        )

    def test_trash_joins_duplicate_queue_and_hides_pending_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace.encrypted(root)
            app = App(workspace)
            handler = Handler.__new__(Handler)
            handler.app = SimpleNamespace(vault=Vault(root, root))
            handler._scoped_app = Mock(return_value=app)
            handler._language = Mock(return_value="en")
            handler._send = Mock()
            handler.headers = Mock()
            handler.headers.get_content_type.return_value = "application/json"
            store = app.duplicate_results()
            self.addCleanup(store.db.close)
            app.catalog = VaultIndex(workspace).initialize()
            app.catalog.add_folder("")
            self.addCleanup(app.close_catalog)
            first, second, third, changed = [root / f"{name}.png" for name in ("a", "b", "c", "d")]
            live_revisions = {}
            with closing(store.copy()) as db:
                for file in (first, second, third, changed):
                    file.write_bytes(b"image")
                    sidecar = sidecar_path(file)
                    sidecar.parent.mkdir(exist_ok=True)
                    sidecar.write_text("tag")
                    _, stored_revision, revision, size = duplicate_file(workspace, file.name)
                    live_revisions[file.name] = revision
                    app.thumbnails.write(file.name, revision, b"preview")
                    app.catalog.set_image(file.name, True, False, 0, "", "tag", {})
                    db.execute("INSERT INTO images VALUES (?, ?, 0, 20, 20, ?)", (file.name, stored_revision, size))
                db.executemany("INSERT INTO pairs VALUES (?, ?, 0)", [(first.name, second.name), (first.name, third.name)])
                store.install(db)
            started, release = threading.Event(), threading.Event()
            remove = app._remove_images
            order = []

            def slow_remove(files, **kwargs):
                order.append(files[0][0])
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release the deletion")
                return remove(files, **kwargs)

            def trash(file):
                handler._delete_image({"scope": "", "path": file.name, "revision": image_revision(file)})
                return handler._send.call_args.args

            with patch.object(app, "_remove_images", side_effect=slow_remove):
                try:
                    pair = app.duplicate_page(0)["pair"]["images"]
                    app.queue_duplicate_review([file[key] for key in ("path", "revision", "live_revision") for file in pair],
                                               "left", "en")
                    self.assertTrue(started.wait(3))
                    self.assertEqual(trash(third), (202, {"ok": True}))
                    self.assertEqual(trash(third)[0], 409)
                    self.assertEqual(trash(changed)[0], 202)
                    changed.write_bytes(b"replacement")
                    self.assertTrue(third.exists())
                    self.assertEqual(app.duplicate_page(0)["count"], 0)
                    self.assertEqual(app.gallery([""])["files"], [first.name])
                    self.assertEqual([item["path"] for item in app.folder_images()["files"]], [first.name])
                finally:
                    release.set()
                with app.review_ready:
                    self.assertTrue(app.review_ready.wait_for(lambda: app.review_worker is None, timeout=3))
            self.assertEqual(order, [second.name, third.name, changed.name])
            self.assertFalse(third.exists())
            self.assertFalse(sidecar_path(third).exists())
            self.assertIsNone(app.thumbnails.read(third.name, live_revisions[third.name]))
            self.assertEqual(store.page(0)["count"], 0)
            self.assertEqual(changed.read_bytes(), b"replacement")
            pending, = app.deletion_status()["items"]
            self.assertEqual(pending["state"], "failed")
            self.assertIn("[187]", pending["message"])

    def test_slow_deletion_leaves_other_pairs_browsable_and_pauses_on_vault_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace.encrypted(root)
            app = App(workspace)
            store = app.duplicate_results()
            self.addCleanup(store.db.close)
            vault = Vault(root, root)
            vault.app = app
            vault.review_identity = b"same-vault"
            vault.process = SimpleNamespace(poll=lambda: None)
            names = ("a.jpg", "b.jpg", "c.jpg", "d.jpg", "e.jpg", "f.jpg")
            with closing(store.copy()) as db:
                for name in names:
                    (root / name).write_bytes(b"image")
                    _, revision, _, size = duplicate_file(workspace, name)
                    db.execute("INSERT INTO images VALUES (?, ?, 0, 20, 20, ?)", (name, revision, size))
                db.executemany("INSERT INTO pairs VALUES (?, ?, 0)", [
                    ("a.jpg", "b.jpg"), ("b.jpg", "c.jpg"), ("c.jpg", "d.jpg"), ("e.jpg", "f.jpg"),
                ])
                store.install(db)

            def current_pair():
                images = app.duplicate_page(0)["pair"]["images"]
                return [image[key] for key in ("path", "revision", "live_revision") for image in images]

            started, release = threading.Event(), threading.Event()
            remove = app._remove_images

            def slow_remove(files, **kwargs):
                started.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release the deletion")
                return remove(files, **kwargs)

            with patch.object(app, "_remove_images", side_effect=slow_remove), patch.object(vault, "_unmount"):
                try:
                    app.queue_duplicate_review(current_pair(), "left", "en")
                    self.assertTrue(started.wait(3))
                    self.assertEqual(app.deletion_status()["items"][0]["state"], "running")
                    self.assertTrue((root / "b.jpg").exists())
                    self.assertEqual(current_pair()[:2], ["c.jpg", "d.jpg"])
                    app.queue_duplicate_review(current_pair(), "both", "en")
                    self.assertEqual(current_pair()[:2], ["e.jpg", "f.jpg"])
                    app.queue_duplicate_review(current_pair(), "left", "en")
                    self.assertEqual(app.duplicate_page(0)["count"], 0)
                    close = threading.Thread(target=vault.close, daemon=True)
                    close.start()
                    with app.review_ready:
                        self.assertTrue(app.review_ready.wait_for(lambda: app.review_paused, timeout=3))
                finally:
                    release.set()
                close.join(timeout=3)
                self.assertFalse(close.is_alive())
            self.assertFalse((root / "b.jpg").exists())
            self.assertTrue((root / "d.jpg").exists())
            self.assertTrue((root / "f.jpg").exists())
            (root / "d.jpg").write_bytes(b"replacement during vault lock")

            reopened = App(workspace)
            vault.app = reopened
            vault.review_identity = b"same-vault"
            vault.process = SimpleNamespace(poll=lambda: None)
            with patch.object(vault, "is_mounted", return_value=True):
                vault.resume_duplicate_reviews()
                with reopened.review_ready:
                    self.assertTrue(reopened.review_ready.wait_for(lambda: reopened.review_worker is None, timeout=3))
            self.addCleanup(reopened.duplicate_store.db.close)
            failures = reopened.deletion_status()["items"]
            self.assertEqual([(item["files"], item["state"]) for item in failures],
                             [(('c.jpg', 'd.jpg'), "failed")])
            self.assertIn("[284]", failures[0]["message"])
            self.assertTrue((root / "c.jpg").exists())
            self.assertEqual((root / "d.jpg").read_bytes(), b"replacement during vault lock")
            self.assertFalse((root / "f.jpg").exists())

    def test_dismissal_persists_and_deletion_rejects_replaced_survivor(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace.encrypted(root)
            for name in ("a.jpg", "b.jpg", "c.jpg"):
                file = root / name
                file.write_bytes(b"fixture")
                tags = sidecar_path(file)
                tags.parent.mkdir(exist_ok=True)
                tags.write_text("tag")
            app = App(workspace)
            store = app.duplicate_results()
            self.addCleanup(store.db.close)
            with closing(store.copy()) as connection:
                for name in ("a.jpg", "b.jpg", "c.jpg"):
                    _, revision, _, size = duplicate_file(workspace, name)
                    connection.execute("INSERT INTO images VALUES (?, ?, 0, 10, 10, ?)", (name, revision, size))
                connection.executemany("INSERT INTO pairs VALUES (?, ?, 0)", [
                    ("a.jpg", "b.jpg"), ("a.jpg", "c.jpg"), ("b.jpg", "c.jpg"),
                ])
                store.install(connection)

            def pair_at(index):
                left, right = app.duplicate_page(index)["pair"]["images"]
                return tuple(item[key] for key in ("path", "revision", "live_revision") for item in (left, right))

            app.review_duplicate(pair_at(0), "different", "en")
            with closing(DuplicateStore(workspace).db) as reopened:
                self.assertEqual(reopened.execute("SELECT COUNT(*) FROM dismissed").fetchone(), (1,))
                self.assertEqual(reopened.execute("SELECT COUNT(*) FROM pairs").fetchone(), (2,))
            pair = pair_at(0)
            original_check = app.check_duplicate_image
            survivor_checked = False

            def replace_survivor(relative, revision, live_revision=None):
                nonlocal survivor_checked
                if relative == "c.jpg" and live_revision is not None:
                    if survivor_checked:
                        (root / "c.jpg").write_bytes(b"replacement for the survivor")
                    survivor_checked = True
                return original_check(relative, revision, live_revision)

            with patch.object(app, "check_duplicate_image", side_effect=replace_survivor):
                with self.assertRaises(DuplicateError) as caught:
                    app.review_duplicate(pair, "right", "en")
            self.assertEqual(caught.exception.code, 284)
            self.assertTrue((root / "a.jpg").exists())
            self.assertTrue(sidecar_path(root / "a.jpg").exists())
            self.assertTrue((root / "b.jpg").exists())

    def test_delete_both_rolls_back_or_clears_both_images_and_their_previews(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = Workspace.encrypted(root)
            app = App(workspace)
            store = app.duplicate_results()
            self.addCleanup(store.db.close)
            names = ("a.jpg", "b.jpg", "c.jpg", "d.jpg")
            live_revisions = {}
            with closing(store.copy()) as connection:
                for name in names:
                    file = root / name
                    file.write_bytes(b"image")
                    tags = sidecar_path(file)
                    tags.parent.mkdir(exist_ok=True)
                    tags.write_text("tag")
                    _, revision, live, size = duplicate_file(workspace, name)
                    live_revisions[name] = live
                    app.thumbnails.write(name, live, b"preview")
                    connection.execute("INSERT INTO images VALUES (?, ?, 0, 10, 10, ?)", (name, revision, size))
                connection.executemany("INSERT INTO pairs VALUES (?, ?, 0)", [
                    ("a.jpg", "b.jpg"), ("a.jpg", "c.jpg"), ("b.jpg", "d.jpg"), ("c.jpg", "d.jpg"),
                ])
                store.install(connection)
            index = Path(workspace.csv)
            sorted_list = Path(workspace.sorted_list)
            index.write_text("file,general\n" + "".join(f"{name},tag\n" for name in names))
            sorted_list.write_text("\n".join(names) + "\n")

            def current_pair():
                images = app.duplicate_page(0)["pair"]["images"]
                return tuple(image[key] for key in ("path", "revision", "live_revision") for image in images)

            pair = current_pair()
            with self.assertRaises(DuplicateError) as stale:
                app.review_duplicate((*pair[:-1], "stale revision"), "both", "en")
            self.assertEqual(stale.exception.code, 284)

            rename = Path.rename

            def fail_second_image(path, destination):
                if path == root / "b.jpg":
                    raise PermissionError("second image cannot be moved")
                return rename(path, destination)

            with patch.object(Path, "rename", fail_second_image), self.assertRaises(DuplicateError) as failed:
                app.review_duplicate(pair, "both", "en")
            self.assertEqual(failed.exception.code, 303)
            for name in names:
                self.assertEqual((root / name).read_bytes(), b"image")
                self.assertTrue(sidecar_path(root / name).exists())
                self.assertEqual(app.thumbnails.read(name, live_revisions[name]), b"preview")
            self.assertEqual(app.duplicate_page(0)["count"], 4)

            self.assertEqual(app.review_duplicate(current_pair(), "both", "en"), "")
            for name in names[:2]:
                self.assertFalse((root / name).exists())
                self.assertFalse(sidecar_path(root / name).exists())
                self.assertIsNone(app.thumbnails.read(name, live_revisions[name]))
            self.assertEqual(index.read_text(), "file,general\nc.jpg,tag\nd.jpg,tag\n")
            self.assertEqual(sorted_list.read_text(), "c.jpg\nd.jpg\n")
            with closing(DuplicateStore(workspace).db) as reopened:
                self.assertEqual(reopened.execute("SELECT left_path, right_path FROM pairs").fetchall(), [("c.jpg", "d.jpg")])
            self.assertEqual(app.thumbnails.read("c.jpg", live_revisions["c.jpg"]), b"preview")
