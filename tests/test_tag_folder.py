import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_folder import (
    DownloadMeter,
    apply_gfx_override,
    byte_progress,
    chunked,
    format_bytes,
    model_status_line,
    progress_line,
    rate_limited,
    tag_images,
    unwrap_results,
)


class TagBatchTests(unittest.TestCase):
    def test_chunked_splits_the_tail(self):
        self.assertEqual(list(chunked([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])

    def test_unwrap_single_dict(self):
        self.assertEqual(unwrap_results({"results": {"general": {"solo": 0.9}}}), [{"general": {"solo": 0.9}}])

    def test_unwrap_batch_list(self):
        rows = [{"general": {"solo": 0.2}}, {"general": {"solo": 0.8}}]
        self.assertEqual(unwrap_results({"results": rows}), rows)

    def test_batch_failure_retries_each_image(self):
        class Tagger:
            def preprocess(self, images):
                if len(images) > 1:
                    raise RuntimeError("batch failed")
                return images

            def forward(self, images):
                return images

            def postprocess(self, images):
                return {"results": {"general": {"solo": 0.5}}}

            def __call__(self, image):
                return {"results": {"general": {"solo": 0.5}}}

        results = tag_images(Tagger(), ["a", "b"])
        self.assertEqual(results, [{"general": {"solo": 0.5}}, {"general": {"solo": 0.5}}])

    def test_blank_gfx_override_is_removed(self):
        os.environ["HSA_OVERRIDE_GFX_VERSION"] = "   "
        self.addCleanup(os.environ.pop, "HSA_OVERRIDE_GFX_VERSION", None)
        self.assertEqual(apply_gfx_override(), "")
        self.assertNotIn("HSA_OVERRIDE_GFX_VERSION", os.environ)

    def test_gfx_override_is_kept(self):
        os.environ["HSA_OVERRIDE_GFX_VERSION"] = " 11.0.0 "
        self.addCleanup(os.environ.pop, "HSA_OVERRIDE_GFX_VERSION", None)
        self.assertEqual(apply_gfx_override(), "11.0.0")
        self.assertEqual(os.environ["HSA_OVERRIDE_GFX_VERSION"], "11.0.0")


class DownloadProgressTests(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(format_bytes(0), "0 Б")
        self.assertEqual(format_bytes(1536), "1.5 КБ")
        self.assertEqual(format_bytes(2 * 1024 ** 3), "2.0 ГБ")

    def test_progress_line_with_and_without_speed(self):
        self.assertEqual(progress_line(256, 1024, None), "Скачивание: 25% (256 Б из 1.0 КБ), скорость ещё считается")
        self.assertEqual(progress_line(2048, 1024, 1024), "Скачивание: 100% (1.0 КБ из 1.0 КБ), сейчас 1.0 КБ/с")
        self.assertEqual(progress_line(2048, 1024, None), "Скачивание: 100% (1.0 КБ из 1.0 КБ), уже в кэше")

    def test_model_status_line(self):
        self.assertEqual(model_status_line(0, 0), "Модель: размер на Hugging Face неизвестен.")
        self.assertIn("50%", model_status_line(512, 1024))

    def test_meter_reports_current_speed_on_a_later_chunk(self):
        now = [0.0]
        lines = []
        meter = DownloadMeter(
            total=1000,
            clock=lambda: now[0],
            emit=lambda line, flush=False: lines.append(line),
            interval=2,
        )
        now[0] = 0.1
        meter.observe("weights", 100, 1000)
        now[0] = 0.4
        meter.observe("weights", 150, 1000)
        now[0] = 3.0
        meter.observe("weights", 550, 1000)
        self.assertEqual(len(lines), 2)
        self.assertIn("скорость ещё считается", lines[0])
        self.assertIn("55%", lines[1])
        self.assertIn("сейчас", lines[1])

    def test_byte_progress_counts_resume_and_new_bytes_once(self):
        meter = DownloadMeter(total=100, clock=lambda: 10.0, emit=lambda line, flush=False: None, interval=100)
        progress = byte_progress(meter, "weights", 100)(total=100, initial=40)
        progress.update(10)
        progress.update(50)
        self.assertEqual(meter.done(), 100)

    def test_two_bars_for_one_file_do_not_pass_its_size(self):
        meter = DownloadMeter(total=1800, cached=0, clock=lambda: 10.0, emit=lambda line, flush=False: None, interval=100)
        first = byte_progress(meter, "model.safetensors", 1800)(total=1800, initial=1800)
        second = byte_progress(meter, "model.safetensors", 1800)(total=1800, initial=0)
        second.update(1800)
        first.update(800)
        self.assertEqual(meter.done(), 1800)

    def test_rate_limited(self):
        self.assertTrue(rate_limited(RuntimeError("HTTPS 429 Too Many Requests")))
        self.assertFalse(rate_limited(RuntimeError("disk full")))


if __name__ == "__main__":
    unittest.main()
