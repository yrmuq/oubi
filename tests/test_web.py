import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from oubi_web import flag_on, parse_progress, preflight, reject_reason, sort_command, tag_command


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
            self.assertIn("--force", tag_command())
            self.assertNotIn("--no-txt", tag_command())
            planned = sort_command(plan=True)
            self.assertIn("--dry-run", planned)
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


if __name__ == "__main__":
    unittest.main()
