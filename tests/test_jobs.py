import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from app import config
from app.jobs import Job, Jobs, playlist_from, too_old
from app.store import Store

FINISHED = "#EXTM3U\n#EXTINF:4.0,\n00000.m4s\n#EXT-X-ENDLIST\n"
PARTIAL = "#EXTM3U\n#EXTINF:4.0,\n00000.m4s\n"
ID = "aqz-KE-bpKQ"

# Three segments, ten seconds, as ffmpeg writes them.
WHOLE = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:EVENT
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init.mp4"
#EXTINF:4.000000,
00000.m4s
#EXTINF:4.000000,
00001.m4s
#EXTINF:2.000000,
00002.m4s
#EXT-X-ENDLIST
"""


def temp_dir(test) -> Path:
    tmp = tempfile.TemporaryDirectory()
    test.addCleanup(tmp.cleanup)
    return Path(tmp.name)


def use_media(test) -> Path:
    """A temporary directory standing in for the volume's media directory."""
    media = temp_dir(test)
    patch = mock.patch.object(config, "MEDIA", media)
    patch.start()
    test.addCleanup(patch.stop)
    return media


class OnDisk(unittest.TestCase):
    def setUp(self):
        self.media = use_media(self)


class Trimming(unittest.TestCase):
    """A playlist that starts part way in, which is how VLC is given a position."""

    def test_from_the_top_keeps_everything(self):
        out = playlist_from(WHOLE, 0)
        self.assertIn("00000.m4s", out)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:0", out)

    def test_drops_whole_segments_before_the_position(self):
        out = playlist_from(WHOLE, 5)
        self.assertNotIn("00000.m4s", out)
        self.assertIn("00001.m4s", out)
        self.assertIn("00002.m4s", out)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", out)

    def test_starts_at_the_segment_the_position_falls_in(self):
        self.assertNotIn("00000.m4s", playlist_from(WHOLE, 4))
        self.assertIn("00001.m4s", playlist_from(WHOLE, 4))

    def test_keeps_what_the_player_cannot_do_without(self):
        out = playlist_from(WHOLE, 5)
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', out)  # fMP4 will not play without it
        self.assertIn("#EXT-X-TARGETDURATION:4", out)
        self.assertIn("#EXT-X-VERSION:7", out)
        self.assertTrue(out.endswith("#EXT-X-ENDLIST\n"), out[-40:])

    def test_past_the_end_leaves_the_last_segment(self):
        out = playlist_from(WHOLE, 10_000)
        self.assertIn("00002.m4s", out)
        self.assertNotIn("00001.m4s", out)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:2", out)


class Skipping(unittest.TestCase):
    """A playlist the sponsors are not in, which is how VLC skips them."""

    def test_nothing_to_skip_is_the_playlist_as_it_was(self):
        self.assertEqual(playlist_from(WHOLE, 0, []), playlist_from(WHOLE, 0))

    def test_leaves_out_the_segments_a_sponsor_covers(self):
        out = playlist_from(WHOLE, 0, [(4, 8)])
        self.assertIn("00000.m4s", out)
        self.assertNotIn("00001.m4s", out)
        self.assertIn("00002.m4s", out)

    def test_says_where_the_timestamps_jump(self):
        # The segment after a gap carries the time it always did, so a player
        # has to be told not to believe it follows on.
        out = playlist_from(WHOLE, 0, [(4, 8)])
        self.assertIn("#EXT-X-DISCONTINUITY\n#EXTINF:2.000000,\n00002.m4s", out)
        self.assertEqual(out.count("#EXT-X-DISCONTINUITY"), 1)

    def test_keeps_a_segment_that_is_more_video_than_sponsor(self):
        self.assertIn("00001.m4s", playlist_from(WHOLE, 0, [(4, 6)]))
        self.assertNotIn("00001.m4s", playlist_from(WHOLE, 0, [(4, 6.5)]))

    def test_a_sponsor_at_the_start_just_starts_later(self):
        out = playlist_from(WHOLE, 0, [(0, 4)])
        self.assertNotIn("00000.m4s", out)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:1", out)
        self.assertNotIn("#EXT-X-DISCONTINUITY", out)  # nothing precedes it to jump from

    def test_a_sponsor_at_the_end_just_ends(self):
        out = playlist_from(WHOLE, 0, [(8, 10)])
        self.assertIn("00001.m4s", out)
        self.assertNotIn("00002.m4s", out)
        self.assertTrue(out.endswith("#EXT-X-ENDLIST\n"), out[-40:])

    def test_starts_past_a_sponsor_the_position_falls_in(self):
        # Sent to VLC from 0:05, which is inside the sponsor: it begins after.
        out = playlist_from(WHOLE, 5, [(4, 8)])
        self.assertNotIn("00001.m4s", out)
        self.assertIn("00002.m4s", out)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:2", out)
        self.assertNotIn("#EXT-X-DISCONTINUITY", out)

    def test_plays_something_rather_than_nothing(self):
        # Covering the whole video is a wrong answer, not an instruction.
        self.assertEqual(playlist_from(WHOLE, 0, [(0, 10)]), playlist_from(WHOLE, 0))

    def test_keeps_what_the_player_cannot_do_without(self):
        out = playlist_from(WHOLE, 0, [(4, 8)])
        self.assertIn('#EXT-X-MAP:URI="init.mp4"', out)
        self.assertIn("#EXT-X-TARGETDURATION:4", out)


class Retention(unittest.TestCase):
    def test_counts_from_the_last_time_it_was_played(self):
        now = time.time()
        with mock.patch.object(config, "UNPLAYED_SECONDS", 3 * 86400):
            self.assertFalse(too_old(now))
            self.assertFalse(too_old(now - 2 * 86400))
            self.assertTrue(too_old(now - 4 * 86400))
            self.assertTrue(too_old(0))  # never recorded counts as long ago
        with mock.patch.object(config, "UNPLAYED_SECONDS", 0):
            self.assertFalse(too_old(0))  # turned off: nothing ages out


class Adopting(OnDisk):
    def left_behind(self, playlist: str, descriptor: str | None) -> Path:
        path = self.media / f"sometoken{len(list(self.media.iterdir()))}"
        path.mkdir()
        (path / "index.m3u8").write_text(playlist)
        (path / "00000.m4s").write_bytes(b"x" * 100)
        if descriptor is not None:
            (path / "job.json").write_text(descriptor)
        return path

    def test_adopts_a_finished_download(self):
        described = json.dumps({"id": ID, "quality": 1080, "started": 1234.5, "title": "A title", "duration": 635})
        job = Job.adopt(self.left_behind(FINISHED, described))
        self.assertEqual((job.video_id, job.quality, job.state), (ID, 1080, "ready"))
        self.assertEqual((job.title, job.duration, job.started), ("A title", 635.0, 1234.5))
        self.assertEqual((job.seconds, job.bytes), (4.0, 100))
        self.assertEqual(job.snapshot()["src"], "/media/sometoken0/index.m3u8")

    def test_falls_back_to_what_it_was_downloaded_with(self):
        # Descriptors written before the home page existed carry neither.
        path = self.left_behind(FINISHED, f'{{"id": "{ID}", "quality": 1080}}')
        (path / "info.json").write_text(json.dumps({"title": "An older download", "duration": 90}))
        job = Job.adopt(path)
        self.assertEqual((job.title, job.duration), ("An older download", 90.0))
        self.assertGreater(job.started, 0)  # the files' own timestamp stands in

    def test_refuses_a_download_cut_short(self):
        self.assertIsNone(Job.adopt(self.left_behind(PARTIAL, f'{{"id": "{ID}", "quality": 1080}}')))

    def test_refuses_one_it_cannot_identify(self):
        self.assertIsNone(Job.adopt(self.left_behind(FINISHED, None)))
        self.assertIsNone(Job.adopt(self.left_behind(FINISHED, "{}")))

    def test_refuses_an_id_that_is_not_one(self):
        # It reaches a subprocess argv, so it is checked coming off disk too.
        self.assertIsNone(Job.adopt(self.left_behind(FINISHED, '{"id": "../../etc/passwd", "quality": 1080}')))


class Listing(OnDisk):
    def setUp(self):
        super().setUp()
        self.jobs = Jobs(None, Store(temp_dir(self) / "state.json"))

    def add(self, video_id: str, started: float, state: str = "ready", seconds: float = 10.0) -> Job:
        job = Job(
            video_id, 720, None, started=started,
            title=f"title of {video_id}", channel=f"channel of {video_id}", duration=100.0,
        )  # fmt: skip
        job.state, job.seconds, job.bytes = state, seconds, 2_000_000
        self.jobs.jobs[video_id, 720] = job
        return job

    def test_newest_download_first(self):
        self.add("aaaaaaaaaaa", 1000)
        self.add("ccccccccccc", 3000)
        self.add("bbbbbbbbbbb", 2000)
        self.assertEqual([v["id"] for v in self.jobs.listing()], ["ccccccccccc", "bbbbbbbbbbb", "aaaaaaaaaaa"])

    def test_leaves_out_what_cannot_be_watched(self):
        self.add("aaaaaaaaaaa", 1000)
        self.add("bbbbbbbbbbb", 2000, state="error")
        self.add("ccccccccccc", 3000, state="expired")
        self.assertEqual([v["id"] for v in self.jobs.listing()], ["aaaaaaaaaaa"])

    def test_carries_what_a_row_shows(self):
        self.add("aaaaaaaaaaa", 1000, state="downloading", seconds=25.0)
        (row,) = self.jobs.listing()
        self.assertEqual((row["title"], row["channel"]), ("title of aaaaaaaaaaa", "channel of aaaaaaaaaaa"))
        self.assertEqual((row["state"], row["seconds"], row["duration"]), ("downloading", 25.0, 100.0))
        self.assertEqual((row["quality"], row["bytes"]), (720, 2_000_000))

    def test_reports_how_far_it_was_played_apart_from_how_far_it_got(self):
        self.add("aaaaaaaaaaa", 1000, state="downloading", seconds=80.0)
        self.jobs.store.touch("aaaaaaaaaaa", 25.0, 100.0)
        (row,) = self.jobs.listing()
        self.assertEqual((row["watched"], row["seconds"]), (25.0, 80.0))

    def test_reports_nothing_watched_for_a_video_nobody_played(self):
        self.add("aaaaaaaaaaa", 1000)
        self.assertEqual(self.jobs.listing()[0]["watched"], 0.0)

    def test_totals_what_it_all_takes_up(self):
        self.add("aaaaaaaaaaa", 1000)
        self.add("bbbbbbbbbbb", 2000)
        summary = self.jobs.summary()
        self.assertEqual(len(summary["videos"]), 2)
        self.assertEqual(summary["bytes"], 4_000_000)
        self.assertGreater(summary["free"], 0)

    def test_totals_nothing_when_there_is_nothing(self):
        self.assertEqual(self.jobs.summary(), {"videos": [], "bytes": 0, "free": mock.ANY})


class Here(OnDisk):
    """Which copy a page is given before anybody asks YouTube anything."""

    def setUp(self):
        super().setUp()
        self.jobs = Jobs(None, Store(temp_dir(self) / "state.json"))

    def add(self, quality: int, state: str = "ready", video_id: str = ID) -> Job:
        job = Job(video_id, quality, None)
        job.state, job.seconds = state, 10.0
        self.jobs.jobs[video_id, quality] = job
        return job

    def test_nothing_here_is_nothing_to_give(self):
        self.assertIsNone(self.jobs.on_disk(ID))

    def test_takes_the_best_already_here(self):
        self.add(480)
        best = self.add(1080)
        self.assertIs(self.jobs.on_disk(ID), best)

    def test_a_page_that_asked_for_a_quality_gets_that_one_or_nothing(self):
        self.add(1080)
        asked = self.add(480)
        self.assertIs(self.jobs.on_disk(ID, 480), asked)
        self.assertIsNone(self.jobs.on_disk(ID, 720))  # must be fetched, not substituted

    def test_passes_over_what_cannot_be_watched(self):
        self.add(1080, state="error")
        self.add(720, state="expired")
        self.assertIsNone(self.jobs.on_disk(ID))
        downloading = self.add(480, state="downloading")
        self.assertIs(self.jobs.on_disk(ID), downloading)  # joining one is still better than asking

    def test_keeps_videos_apart(self):
        mine = self.add(1080)
        self.add(1080, video_id="bbbbbbbbbbb")
        self.assertIs(self.jobs.on_disk(ID), mine)


class Describing(unittest.IsolatedAsyncioTestCase):
    """A video already here answers the page from its own dump, not YouTube."""

    def setUp(self):
        self.media = use_media(self)

    def left_behind(self, dump) -> Job:
        job = Job(ID, 1080, None)
        job.dir.mkdir(parents=True)
        if dump is not None:
            (job.dir / "info.json").write_text(dump if isinstance(dump, str) else json.dumps(dump))
        return job

    async def test_describes_itself_from_the_dump_it_was_downloaded_with(self):
        job = self.left_behind({
            "id": ID,
            "title": "A title",
            "channel": "A channel",
            "duration": 635,
            "formats": [
                {"format_id": "137", "format_note": "1080p", "vcodec": "avc1", "acodec": "none", "protocol": "https"},
                {"format_id": "140", "vcodec": "none", "acodec": "mp4a.40.2", "protocol": "https"},
            ],
        })
        info = await job.described()
        self.assertEqual(info.meta["title"], "A title")
        self.assertEqual(info.meta["duration"], 635)
        self.assertEqual(info.meta["qualities"], [1080])

    async def test_parses_it_once(self):
        job = self.left_behind({"id": ID, "duration": 1, "formats": [
            {"format_id": "18", "format_note": "360p", "vcodec": "avc1", "acodec": "mp4a.40.2", "protocol": "https"},
        ]})
        first = await job.described()
        (job.dir / "info.json").unlink()  # gone: a second read would fail
        self.assertIs(await job.described(), first)

    async def test_a_dump_it_cannot_read_sends_the_page_back_to_youtube(self):
        self.assertIsNone(await self.left_behind(None).described())
        self.assertIsNone(await self.left_behind("not json").described())
        self.assertIsNone(await self.left_behind({"id": ID, "formats": []}).described())

    async def test_a_download_in_flight_answers_from_what_it_already_has(self):
        job = Job(ID, 1080, None)
        job.info = "the extraction this download is running on"
        self.assertEqual(await job.described(), job.info)


class Removing(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        use_media(self)
        self.jobs = Jobs(None, Store(temp_dir(self) / "state.json"))

    async def test_takes_the_files_and_keeps_the_position(self):
        job = Job("aaaaaaaaaaa", 720, None, title="A title", duration=100.0)
        job.state, job.bytes = "ready", 2_000_000
        self.jobs.jobs["aaaaaaaaaaa", 720] = job
        self.jobs.store.touch("aaaaaaaaaaa", 30.0, 100.0)

        self.assertTrue(self.jobs.remove("aaaaaaaaaaa", 720))
        self.assertEqual(self.jobs.listing(), [])
        # An open page is told it is gone, rather than fetching it again.
        self.assertEqual(job.state, "expired")
        self.assertEqual(self.jobs.store.watched("aaaaaaaaaaa"), 30.0)
        await asyncio.gather(*self.jobs._deleting, return_exceptions=True)

    async def test_says_so_when_there_is_nothing_to_take(self):
        self.assertFalse(self.jobs.remove("aaaaaaaaaaa", 720))
        self.assertFalse(self.jobs.remove("bbbbbbbbbbb", 1080))


if __name__ == "__main__":
    unittest.main()
