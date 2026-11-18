import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from oubi_duplicates import DuplicateError, DuplicateStore, duplicate_file, matching_pairs
from oubi_files import sidecar_path
from oubi_web import App, Workspace


class DuplicateTests(unittest.TestCase):
    def test_band_search_returns_direct_matches_only(self):
        entries = [("a", "", 0), ("b", "", 255), ("c", "", 511), ("d", "", 0)]
        self.assertEqual(
            set(matching_pairs(entries, lambda: False)),
            {("a", "b", 8), ("b", "c", 1), ("a", "d", 0), ("b", "d", 8)},
        )

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
