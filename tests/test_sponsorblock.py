import json
import tempfile
import unittest
from pathlib import Path

from app import sponsorblock


def temp_dir(test) -> Path:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    return Path(tmp.name)


class Ranges(unittest.TestCase):
    """What a playlist is cut with, out of what SponsorBlock answered."""

    def test_soonest_first(self):
        found = [{"start": 30, "end": 40}, {"start": 5, "end": 10}]
        self.assertEqual(sponsorblock.ranges(found), [(5.0, 10.0), (30.0, 40.0)])

    def test_merges_what_overlaps(self):
        # A sponsor read out over the intro is both, and a segment of video
        # decides whether to go by how much of it is covered.
        found = [{"start": 0, "end": 12}, {"start": 8, "end": 20}]
        self.assertEqual(sponsorblock.ranges(found), [(0.0, 20.0)])

    def test_merges_what_merely_touches(self):
        found = [{"start": 0, "end": 10}, {"start": 10, "end": 20}]
        self.assertEqual(sponsorblock.ranges(found), [(0.0, 20.0)])

    def test_leaves_a_gap_alone(self):
        found = [{"start": 0, "end": 10}, {"start": 11, "end": 20}]
        self.assertEqual(sponsorblock.ranges(found), [(0.0, 10.0), (11.0, 20.0)])

    def test_drops_a_range_that_is_not_one(self):
        self.assertEqual(sponsorblock.ranges([{"start": 10, "end": 10}, {"start": 20, "end": 5}]), [])

    def test_nothing_found_is_nothing_to_cut(self):
        self.assertEqual(sponsorblock.ranges([]), [])


class Kept(unittest.TestCase):
    """Beside the video, because VLC asks for the playlist again and again."""

    def setUp(self):
        self.dir = temp_dir(self)

    def test_comes_back_ready_to_cut_with(self):
        sponsorblock.remember(self.dir, [{"start": 5, "end": 10, "category": "sponsor"}])
        self.assertEqual(sponsorblock.remembered(self.dir), [(5.0, 10.0)])

    def test_nothing_kept_means_nothing_skipped(self):
        self.assertEqual(sponsorblock.remembered(self.dir), [])

    def test_survives_a_download_from_before_there_was_any(self):
        (self.dir / sponsorblock.SEGMENTS).write_text("not json at all")
        self.assertEqual(sponsorblock.remembered(self.dir), [])

    def test_survives_an_answer_it_cannot_read(self):
        (self.dir / sponsorblock.SEGMENTS).write_text(json.dumps([{"begin": 5}]))
        self.assertEqual(sponsorblock.remembered(self.dir), [])

    def test_the_latest_answer_replaces_the_last(self):
        sponsorblock.remember(self.dir, [{"start": 5, "end": 10}])
        sponsorblock.remember(self.dir, [{"start": 30, "end": 40}])
        self.assertEqual(sponsorblock.remembered(self.dir), [(30.0, 40.0)])


if __name__ == "__main__":
    unittest.main()
