import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import store
from app.store import Store

ID = "aqz-KE-bpKQ"


class Positions(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "state.json"

    def test_resumes_only_from_the_middle(self):
        s = Store(self.path)
        s.touch(ID, 90, 600)
        self.assertEqual(s.resume(ID), 90)
        s.touch(ID, 5, 600)
        self.assertEqual(s.resume(ID), 0)  # barely started
        s.touch(ID, 595, 600)
        self.assertEqual(s.resume(ID), 0)  # watched to the end
        self.assertEqual(s.resume("dQw4w9WgXcQ"), 0)  # never opened

    def test_watching_to_the_end_leaves_nothing_to_resume(self):
        s = Store(self.path)
        s.touch(ID, 595, 600)
        self.assertEqual(s.resume(ID), 0)  # the player starts at the top
        self.assertEqual(s.watched(ID), 595)  # the list still shows it as watched
        self.assertEqual(s.watched("dQw4w9WgXcQ"), 0)

    def test_opening_again_keeps_the_position(self):
        s = Store(self.path)
        s.touch(ID, 90, 600)
        s.seen[ID].played = time.time() - 60
        s.touch(ID)
        self.assertEqual(s.resume(ID), 90)
        self.assertGreater(s.last_played(ID), time.time() - 5)

    def test_survives_a_restart(self):
        s = Store(self.path)
        s.touch(ID, 90, 600)
        asyncio.run(s.flush())
        self.assertEqual(Store(self.path).resume(ID), 90)

    def test_ignores_an_unreadable_file(self):
        self.path.write_text("{ not json")
        self.assertEqual(Store(self.path).seen, {})

    def test_keeps_only_the_newest_positions(self):
        s = Store(self.path)
        for i, video_id in enumerate(("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc")):
            s.touch(video_id, 60, 600)
            s.seen[video_id].played = 1000 + i
        with mock.patch.object(store, "MAX_POSITIONS", 2):
            asyncio.run(s.flush())
        self.assertEqual(set(Store(self.path).seen), {"bbbbbbbbbbb", "ccccccccccc"})


if __name__ == "__main__":
    unittest.main()
