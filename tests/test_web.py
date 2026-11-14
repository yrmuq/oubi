import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oubi_web import (
    absolute_folders,
    flag_on,
    folder_names,
    mark,
    parse_progress,
    preflight,
    reject_reason,
    resolve_under,
    sort_command,
    summarize_images,
    tag_command,
)
from sort_images import under_roots


class WebTests(unittest.TestCase):
    def test_parse_download_tag_and_sort(self):
        self.assertEqual(parse_progress("Скачивание: 25% (1.0 КБ из 4.0 КБ), сейчас 1.0 КБ/с")["percent"], 25)
        self.assertEqual(parse_progress("Разметка: 10% (2 из 20), 0.20 карт./с, осталось ~1.0 ч")["kind"], "tag")
        self.assertEqual(parse_progress("Раскладка: 25 из 100")["percent"], 25)
        self.assertEqual(parse_progress("Сейчас: размечаю a.jpg.")["step"], "Сейчас: размечаю a.jpg.")

    def test_tag_waits_for_the_model_and_a_free_job(self):
        self.assertIsNone(reject_reason("ready", False, "tag"))
        self.assertIsNotNone(reject_reason("downloading", False, "tag"))
        self.assertIsNone(reject_reason("downloading", False, "sort"))
        self.assertIsNotNone(reject_reason("ready", True, "plan"))

    def test_commands_follow_the_switches(self):
        previous = os.environ.copy()
        os.environ["OUBI_BATCH_SIZE"] = "1"
        os.environ["OUBI_NO_TXT"] = "0"
        os.environ["OUBI_FORCE"] = "1"
        os.environ["OUBI_MOVE"] = "0"
        os.environ["OUBI_NSFW_FROM"] = "e"
        os.environ["OUBI_COPY_TXT"] = "0"
        try:
            self.assertIn("/images/one", tag_command(["/images/one"]))
            self.assertIn("--force", tag_command(["/images/one"]))
            self.assertNotIn("--no-txt", tag_command(["/images/one"]))
            planned = sort_command(plan=True, folders=["/images/one"])
            self.assertIn("--dry-run", planned)
            self.assertIn("--only", planned)
            self.assertNotIn("--sorted-list", planned)
            self.assertIn("--sorted-list", sort_command(plan=False, folders=["/images/one"]))
            self.assertIn("e", planned)
            self.assertNotIn("--move", planned)
            self.assertTrue(flag_on("yes"))
            self.assertFalse(flag_on("0"))
        finally:
            os.environ.clear()
            os.environ.update(previous)

    def test_preflight_requires_the_mounts(self):
        self.assertIn("/images", preflight("tag"))
        self.assertIn("/output", preflight("sort"))

    def test_folder_marks_and_roots(self):
        self.assertEqual(mark(0, 4), "none")
        self.assertEqual(mark(1, 4), "part")
        self.assertEqual(mark(4, 4), "done")
        self.assertTrue(under_roots("/images/a/x.jpg", ["/images/a"]))
        self.assertFalse(under_roots("/images/ab/x.jpg", ["/images/a"]))
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            folder = base / "pack"
            folder.mkdir()
            image = folder / "a.jpg"
            image.write_bytes(b"jpg")
            image.with_suffix(".txt").write_text("1girl", encoding="utf-8")
            self.assertEqual(resolve_under(base, "pack"), folder.resolve())
            with self.assertRaises(ValueError):
                resolve_under(base, "../outside")
            info = summarize_images("pack", [image], set(), set())
            self.assertEqual(info["scan"], "done")
            self.assertEqual(info["sort"], "none")
            info = summarize_images("pack", [image], set(), {str(image.resolve())})
            self.assertEqual(info["sort"], "done")
            paths, error = absolute_folders(["pack"], base)
            self.assertIsNone(error)
            self.assertEqual(paths, [str(folder.resolve())])
            self.assertEqual(absolute_folders([], base)[1], "Нужно выбрать папки.")
            (base / "b.jpg").write_bytes(b"jpg")
            self.assertEqual(folder_names(base / "empty" if False else base), ["pack"])
            only = base / "loose"
            only.mkdir()
            (only / "c.jpg").write_bytes(b"jpg")
            self.assertEqual(folder_names(only), [""])


if __name__ == "__main__":
    unittest.main()
