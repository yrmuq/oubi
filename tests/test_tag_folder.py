import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tag_folder import apply_gfx_override, chunked, tag_images, unwrap_results


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


if __name__ == "__main__":
    unittest.main()
