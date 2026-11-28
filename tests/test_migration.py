import csv
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from oubi_files import sidecar_path
from oubi_migration import MigrationError, migrate, migration_options, move_into_empty_path
from oubi_web import App, Workspace


class MigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "Album"
        self.source.mkdir()
        self.workspace = Workspace.encrypted(self.root)

    def image(self, name, month, year=2025, tagged=False):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
        when = datetime(year, month, 15, 12).timestamp()
        os.utime(path, (when, when))
        if tagged:
            sidecar = sidecar_path(path)
            sidecar.parent.mkdir(exist_ok=True)
            sidecar.write_text("moon", encoding="utf-8")
        return path

    def run_migration(self, recursive=False, destination=None):
        return migrate(self.workspace, self.source, destination or self.source, recursive, 2025,
                       cancelled=lambda: False, progress=lambda done, total: None, before_move=lambda: None)

    def test_direct_images_and_tags_move_and_index_paths_follow(self):
        paths = [self.image(f"{month}.jpg", month, tagged=True) for month in (1, 4, 7, 10)]
        paths.append(self.image("old.jpg", 4, 2024))
        untouched = self.image("child/skip.jpg", 5)
        destination = self.root / "Destination"
        destination.mkdir()
        index = Path(self.workspace.csv)
        history = Path(self.workspace.sorted_list)
        with index.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows([("file", "general"), (str(paths[0]), "moon"),
                                          (str(paths[1].relative_to(self.root)), "stars")])
        history.write_text(str(paths[0]) + "\n", encoding="utf-8")
        remembered_mtime = paths[0].stat().st_mtime

        moved, skipped, pending = self.run_migration(destination=destination)

        self.assertEqual([moved[path].parent.name for path in paths],
                         ["2025-Q1", "2025-Q2", "2025-Q3", "2025-Q4", "2025-other"])
        self.assertEqual(skipped, [])
        self.assertFalse(pending)
        self.assertTrue(untouched.exists())
        self.assertFalse(paths[0].exists())
        self.assertFalse(sidecar_path(paths[0]).exists())
        self.assertEqual(sidecar_path(moved[paths[0]]).read_text(encoding="utf-8"), "moon")
        self.assertEqual(moved[paths[0]].stat().st_mtime, remembered_mtime)
        with index.open(encoding="utf-8", newline="") as handle:
            self.assertEqual(list(csv.reader(handle)), [["file", "general"], [str(moved[paths[0]]), "moon"],
                                                        ["Destination/2025-Q2/4.jpg", "stars"]])
        self.assertEqual(history.read_text(encoding="utf-8"), str(moved[paths[0]]) + "\n")

    def test_recursive_majority_preserves_trees_and_reports_ties_and_conflicts(self):
        quarter = self.image("Trip/Deep/spring.jpg", 4, tagged=True)
        self.image("Trip/may.jpg", 5)
        self.image("Trip/earlier.jpg", 1)
        outside = self.image("Archive/old.jpg", 4, 2023)
        self.image("Archive/new.jpg", 8, 2026)
        self.image("Archive/current.jpg", 4)
        tie = self.image("Tie/first.jpg", 1)
        self.image("Tie/second.jpg", 4)
        collision = self.image("collision.jpg", 4, tagged=True)
        existing = self.image("2025-Q2/collision.jpg", 4)
        out = self.image("Inside/Target/already.jpg", 4)

        moved, skipped, _ = self.run_migration(recursive=True)

        self.assertEqual(moved[quarter].relative_to(self.source).as_posix(), "2025-Q2/Trip/Deep/spring.jpg")
        self.assertTrue(sidecar_path(moved[quarter]).is_file())
        self.assertEqual(moved[outside].relative_to(self.source).as_posix(), "2025-other/Archive/old.jpg")
        self.assertEqual(skipped, [{"path": "Album/collision.jpg", "code": 392},
                                   {"path": "Album/Tie", "code": 391}])
        self.assertTrue(tie.is_file())
        self.assertTrue(collision.is_file() and existing.is_file())
        self.assertEqual(moved[out].relative_to(self.source).as_posix(), "2025-Q2/Inside/Target/already.jpg")
        self.assertNotIn(existing, moved)

    def test_move_failure_restores_images_and_sidecars(self):
        first = self.image("first.jpg", 1, tagged=True)
        second = self.image("second.jpg", 2, tagged=True)

        def fail_after_first(source, destination):
            if source == second:
                raise PermissionError("cannot rename second image")
            move_into_empty_path(source, destination)

        with patch("oubi_migration.move_into_empty_path", side_effect=fail_after_first):
            with self.assertRaises(PermissionError):
                self.run_migration()

        for path in (first, second):
            self.assertTrue(path.is_file() and sidecar_path(path).is_file())
        self.assertEqual(list(self.source.glob("2025-*")), [])

    def test_rejects_paths_outside_the_active_workspace(self):
        with self.assertRaises(MigrationError) as raised:
            migration_options(self.workspace, {"year": 2025, "source": "../outside", "recursive": True,
                                               "destination": None, "order": "modified"})
        self.assertEqual(raised.exception.code, 397)

    def test_job_rebuilds_the_vault_catalog_at_the_new_paths(self):
        self.image("spring.jpg", 4, tagged=True)
        app = App(self.workspace)
        self.addCleanup(app.close_catalog)
        app._count_folders(threading.Event(), "initial")
        self.assertEqual(app.gallery([""]), {"files": ["Album/spring.jpg"]})

        job_id = app.start_migration((self.source, self.source, False, 2025), "en")
        app.migration_worker.join(timeout=3)
        with app.lock:
            scan = app.folder_scan
        if scan:
            scan[1].join(timeout=3)

        expected = "Album/2025-Q2/spring.jpg"
        self.assertEqual(app.gallery([""], ["moon"]), {"files": [expected]})
        self.assertEqual(app.folder_images("Album/2025-Q2"), {"files": [{"path": expected, "tagged": True}]})
        self.assertEqual(app.status()["migration"], {"id": job_id, "running": False, "moved": 1,
                                                      "skipped": [], "warning": "", "error": ""})
