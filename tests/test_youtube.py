import unittest

from app.jobs import playlist_seconds
from app.updater import newest
from app.youtube import choose_formats, error_message, parse_video_id


def video(id, q, codec, **kw):
    return {"format_id": id, "format_note": f"{q}p", "vcodec": codec, "acodec": "none", "protocol": "https", **kw}


def audio(id, codec="mp4a.40.2", **kw):
    return {"format_id": id, "vcodec": "none", "acodec": codec, "protocol": "https", **kw}


class ParseVideoId(unittest.TestCase):
    def test_accepts(self):
        for text in (
            "aqz-KE-bpKQ",
            "https://www.youtube.com/watch?v=aqz-KE-bpKQ&t=10s",
            "youtube.com/watch?app=desktop&v=aqz-KE-bpKQ",
            "https://youtu.be/aqz-KE-bpKQ?si=x",
            "https://m.youtube.com/shorts/aqz-KE-bpKQ",
            "https://www.youtube-nocookie.com/embed/aqz-KE-bpKQ",
            "http://192.168.1.2:3000/watch?v=aqz-KE-bpKQ",
        ):
            self.assertEqual(parse_video_id(text), "aqz-KE-bpKQ", text)

    def test_rejects(self):
        for text in ("", "https://youtube.com/@Blender", "file:///etc/passwd", "--exec=x", "https://[", "https://example.com/aqz-KE-bpKQ"):
            self.assertIsNone(parse_video_id(text), text)


class ChooseFormats(unittest.TestCase):
    def test_prefers_h264_up_to_1080_and_vp9_above(self):
        c = choose_formats(
            [
                video("h", 1080, "avc1.64002A", filesize=10), video("v", 1080, "vp9"),
                video("v4k", 2160, "vp9"), video("a4k", 2160, "av01.0.13M.08"), audio("140", filesize=5),
            ],
            60,
        )  # fmt: skip
        self.assertEqual(c[1080].formats, ["h", "140"])
        self.assertEqual(c[1080].size, 15)
        self.assertEqual(c[2160].formats, ["v4k", "140"])

    def test_audio_prefers_original_stereo_aac_without_drc(self):
        c = choose_formats(
            [
                video("h", 720, "avc1"), audio("140-drc", abr=129), audio("140", abr=129),
                audio("258", abr=387, audio_channels=6), audio("251", "opus", abr=160),
                audio("140-1", abr=200, language_preference=-10),
            ],
            60,
        )  # fmt: skip
        self.assertEqual(c[720].formats, ["h", "140"])

    def test_vertical_video_uses_short_side(self):
        f = video("h", 0, "avc1", width=1080, height=1920)
        f["format_note"] = ""
        self.assertIn(1080, choose_formats([f, audio("140")], 60))

    def test_combined_only(self):
        f = video("18", 360, "avc1", acodec="mp4a.40.2")
        self.assertEqual(choose_formats([f], 60)[360].formats, ["18"])


class Misc(unittest.TestCase):
    def test_newest(self):
        index = {"files": [
            {"filename": "yt_dlp-2026.10.26-py3-none-any.whl"},
            {"filename": "yt_dlp-2026.11.21.232659.dev0-py3-none-any.whl"},
            {"filename": "yt_dlp-2026.12.1-py3-none-any.whl", "yanked": True},
            {"filename": "yt_dlp-2026.12.2.tar.gz"},
        ]}  # fmt: skip
        self.assertEqual(str(newest(index, "nightly")), "2026.11.21.232659.dev0")
        self.assertEqual(str(newest(index, "stable")), "2026.10.26")

    def test_error_message(self):
        self.assertEqual(
            error_message("ERROR: [youtube] aqz-KE-bpKQ: Private video. Use --cookies-from-browser"), "Private video."
        )

    def test_playlist_seconds(self):
        self.assertAlmostEqual(playlist_seconds("#EXTM3U\n#EXTINF:4.5,\na\n#EXTINF:2.25,\nb\n"), 6.75)


if __name__ == "__main__":
    unittest.main()
