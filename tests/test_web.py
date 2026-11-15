import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oubi_web import (
    App,
    absolute_folders,
    first_character,
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
    read_theme,
    write_theme,
    read_genres,
    write_genres,
    read_layout,
    write_layout,
    images_with_tag,
    detect_lang,
    read_lang,
    write_lang,
    load_catalogs,
    text,
    placeholder_names,
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
        self.assertEqual(first_character("hatsune_miku, other"), "hatsune_miku")
        self.assertEqual(first_character(""), "")
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
            bare = folder / "b.jpg"
            bare.write_bytes(b"jpg")
            info = summarize_images("pack", [bare], {"pack/b.jpg"}, set(), base)
            self.assertEqual(info["scan"], "done")
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

    def test_folder_method_stays_callable(self):
        self.assertTrue(callable(App().folders))

    def test_theme_is_kept_on_disk(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "theme"
            self.assertEqual(read_theme(path), "auto")
            self.assertTrue(write_theme("dark", path))
            self.assertEqual(read_theme(path), "dark")
            self.assertFalse(write_theme("blue", path))
            self.assertEqual(read_theme(path), "dark")

    def test_page_genres_override_compose(self):
        import tempfile
        previous = os.environ.get("OUBI_GENRE_1")
        os.environ["OUBI_GENRE_1"] = "portrait"
        os.environ["OUBI_GENRE_2"] = ""
        os.environ["OUBI_GENRE_3"] = ""
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "genres.txt"
                self.assertEqual(read_genres(path)[0], "portrait")
                self.assertTrue(write_genres(["comic", "scenery", ""], path))
                self.assertEqual(read_genres(path), ["comic", "scenery"])
                self.assertTrue(write_genres(["x"] * 20, path))
                self.assertEqual(len(read_genres(path)), 20)
                self.assertFalse(write_genres(["y"] * 21, path))
                self.assertEqual(len(read_genres(path)), 20)
                os.environ["OUBI_GENRE_20"] = "last"
                empty = Path(tmp) / "missing.txt"
                self.assertEqual(read_genres(empty)[-1], "last")
                os.environ.pop("OUBI_GENRE_20", None)
        finally:
            if previous is None:
                os.environ.pop("OUBI_GENRE_1", None)
            else:
                os.environ["OUBI_GENRE_1"] = previous


class LayoutTests(unittest.TestCase):
    def test_layout_is_kept_on_disk(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "layout.json"
            self.assertEqual(read_layout(path), {})
            self.assertTrue(write_layout({"left": 320, "mid": 400.5, "nope": 1}, path))
            self.assertEqual(read_layout(path), {"left": 320, "mid": 400})
            self.assertFalse(write_layout({"left": 10}, path))
            self.assertEqual(read_layout(path)["left"], 320)
            order = {
                "left": ["log"],
                "mid": ["buttons", "folders", "progress"],
                "right": ["preview", "genres", "info"],
            }
            self.assertTrue(write_layout({"blocks": order}, path))
            self.assertEqual(read_layout(path)["blocks"], order)
            self.assertFalse(write_layout({"blocks": {"left": ["log"]}}, path))
            self.assertEqual(read_layout(path)["blocks"]["left"], ["log"])
            self.assertTrue(write_layout({"heights": {"folders": 240, "nope": 10}}, path))
            self.assertEqual(read_layout(path)["heights"], {"folders": 240})

    def test_images_with_tag(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "images"
            root.mkdir()
            picture = root / "a.jpg"
            picture.write_bytes(b"x")
            other = root / "b.jpg"
            other.write_bytes(b"x")
            csv_path = Path(tmp) / "tags.csv"
            csv_path.write_text(
                "file,rating,character,copyright,style,general,meta\n"
                f"{picture},rating:s,,original,,comic,1girl\n"
                f"{other},rating:s,,original,,portrait,1girl\n",
                encoding="utf-8",
            )
            self.assertEqual(images_with_tag("comic", csv_path, root), ["a.jpg"])
            self.assertEqual(images_with_tag("missing", csv_path, root), [])

    def test_language_detects_browser_and_keeps_a_choice(self):
        import tempfile
        self.assertEqual(detect_lang("ru-RU,ru;q=0.9,en;q=0.8"), "ru")
        self.assertEqual(detect_lang("fr-FR,fr;q=0.9"), "en")
        self.assertEqual(detect_lang("", {"LANG": "ru_RU.UTF-8"}), "ru")
        packs = load_catalogs()
        self.assertEqual(packs["ru"]["1"], "Опознать")
        self.assertEqual(packs["en"]["11"], "filename")
        self.assertEqual(packs["ru"]["11"], "filename")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "language"
            self.assertEqual(read_lang(path, "fr"), "en")
            self.assertTrue(write_lang("ru", path))
            self.assertEqual(read_lang(path, "en-US"), "ru")
            self.assertFalse(write_lang("de", path))
        self.assertNotEqual(placeholder_names("Опознать {missing}"), placeholder_names("Identify"))
        self.assertIn("1", packs["en"])
        self.assertTrue(text(1))


if __name__ == "__main__":
    unittest.main()
