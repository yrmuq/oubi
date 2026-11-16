import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oubi_files import ImageChangedError, delete_source_image, image_revision, sidecar_path


class PreviewDeletionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.images = self.root / "images"
        self.images.mkdir()
        self.index = self.root / "tags.csv"
        self.sorted_list = self.root / "sorted.txt"
        self.image = self.images / "photo.jpg"
        self.image.write_bytes(b"image")
        self.tags = sidecar_path(self.image)
        self.tags.parent.mkdir()
        self.tags.write_text("moon", encoding="utf-8")

    def delete(self, relative="photo.jpg", revision=None):
        return delete_source_image(self.images, relative, revision or image_revision(self.image),
                                   index=self.index, sorted_list=self.sorted_list)

    def write_indexes(self, sibling):
        with self.index.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerows([["file", "general"], [str(self.image), "moon"], ["./photo.jpg", "older"],
                              [str(sibling), "rain, night"]])
        self.sorted_list.write_text(f"{self.image}\nphoto.jpg\n{sibling}\n", encoding="utf-8")

    def test_removes_only_exact_image_and_its_records(self):
        other = self.images / "photo.png"
        other.write_bytes(b"other")
        sidecar_path(other).write_text("rain", encoding="utf-8")
        self.write_indexes(other)
        with self.index.open("a", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerow([" photo.jpg", "leading space"])
        with self.sorted_list.open("a", encoding="utf-8") as handle:
            handle.write(" photo.jpg\n")

        self.assertFalse(self.delete())
        self.assertFalse(self.image.exists())
        self.assertFalse(self.tags.exists())
        self.assertEqual(other.read_bytes(), b"other")
        self.assertEqual(sidecar_path(other).read_text(), "rain")
        with self.index.open(newline="", encoding="utf-8") as handle:
            self.assertEqual(list(csv.reader(handle)), [
                ["file", "general"], [str(other), "rain, night"], [" photo.jpg", "leading space"],
            ])
        self.assertEqual(self.sorted_list.read_text(), f"{other}\n photo.jpg\n")
        self.assertEqual(list(self.root.rglob(".oubi-*")), [])

    def test_rejects_modified_image_and_accepts_missing_sidecar(self):
        original = image_revision(self.image)
        self.image.write_bytes(b"replacement")
        with self.assertRaises(ImageChangedError):
            self.delete(revision=original)
        self.assertEqual(self.image.read_bytes(), b"replacement")
        self.assertTrue(self.tags.exists())
        self.tags.unlink()
        self.assertFalse(self.delete())
        with self.assertRaises(ImageChangedError):
            self.delete(revision=original)

    def test_rejects_traversal_and_symbolic_links(self):
        outside = self.root / "outside.jpg"
        outside.write_bytes(b"keep")
        (self.images / "alias.jpg").symlink_to(outside)
        for path in ("../outside.jpg", str(outside), "alias.jpg", "tags/photo.jpg.txt"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.delete(path, image_revision(outside))
        self.assertEqual(outside.read_bytes(), b"keep")
        self.assertTrue(self.image.exists())

    def test_restores_image_sidecar_and_index_if_an_index_replace_fails(self):
        other = self.images / "other.jpg"
        self.write_indexes(other)
        old_index = self.index.read_bytes()
        real_replace = Path.replace

        def replace(path, destination):
            if Path(destination) == self.sorted_list and path.name == "updated":
                raise PermissionError("fixture failure")
            return real_replace(path, destination)

        with patch.object(Path, "replace", replace):
            with self.assertRaises(PermissionError):
                self.delete()
        self.assertEqual(self.image.read_bytes(), b"image")
        self.assertEqual(self.tags.read_text(), "moon")
        self.assertEqual(self.index.read_bytes(), old_index)
        self.assertIn("photo.jpg", self.sorted_list.read_text())
        self.assertEqual(list(self.root.rglob(".oubi-*")), [])
