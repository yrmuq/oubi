import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sort_images import (
    genre_folder,
    group_series,
    majority_tag,
    series_folder_name,
    series_targets,
    stick_character,
)


def page(char, girls="1girl", copy="evangelion"):
    return {
        "file": "",
        "_chars": [char] if char else [],
        "_gen": {girls} if girls else set(),
        "_copy": [copy] if copy else [],
        "_rating": ["rating:g"],
        "character": char,
    }


class SeriesTests(unittest.TestCase):
    def test_pages_group_and_three_digit_number_stays_out(self):
        stem = "/images/pack/138092568-1e90743ef45f5c9b5e3f02759210c71c_p{}_master1200.jpg"
        paths = [stem.format(n) for n in (1, 2, 3, 4, 5)]
        paths.append(stem.format(100))
        paths.append("/images/pack/other.jpg")
        groups = group_series(paths)
        self.assertEqual(len(groups), 1)
        self.assertEqual(set(groups[0]["files"].values()), {1, 2, 3, 4, 5})
        self.assertNotIn("#", series_folder_name(groups[0]["key"]))

    def test_majority_character_or_group(self):
        rows = [page("asuka"), page("asuka"), page("asuka"), page("rei"), page("")]
        self.assertEqual(majority_tag(rows, {}), "asuka")
        even = [page("asuka"), page("asuka"), page("rei"), page("rei")]
        self.assertIsNone(majority_tag(even, {}))
        row = page("rei")
        self.assertTrue(stick_character(row, "asuka"))
        self.assertEqual(row["_chars"], ["asuka", "rei"])
        self.assertFalse(stick_character(row, "asuka"))

    def test_series_goes_inside_the_character_folder_by_page_number(self):
        rows = []
        for number, char in ((1, "asuka"), (2, "asuka"), (3, "asuka"), (4, "rei"), (5, "")):
            row = page(char)
            row["file"] = f"/images/pack/post_p{number}_master1200.jpg"
            rows.append(row)
        targets, stuck = series_targets(rows, {}, {"rating:q", "rating:e"}, lambda char, row: "evangelion")
        self.assertEqual(targets[rows[0]["file"]], ("evangelion", "sfw", "asuka", "", "post_p_master1200", "1.jpg"))
        self.assertEqual(targets[rows[4]["file"]][-1], "5.jpg")
        self.assertIn(rows[4]["file"], {path for path, _tag in stuck})
        split = [page("asuka"), page("asuka"), page("rei"), page("rei")]
        for number, row in enumerate(split, 1):
            row["file"] = f"/images/pack/post_p{number}.jpg"
        targets, stuck = series_targets(split, {}, {"rating:q", "rating:e"}, lambda char, row: "evangelion")
        self.assertEqual(targets[split[0]["file"]][2], "group")
        self.assertEqual(stuck, [])

    def test_original_uses_the_first_matching_genre(self):
        tags = {"portrait", "comic"}
        self.assertEqual(genre_folder("original", tags, ["comic", "portrait"]), "comic")
        self.assertEqual(genre_folder("original", {"1girl"}, ["comic"]), "other")
        self.assertEqual(genre_folder("evangelion", tags, ["comic"]), "")
        self.assertEqual(genre_folder("original", tags, []), "")


if __name__ == "__main__":
    unittest.main()
