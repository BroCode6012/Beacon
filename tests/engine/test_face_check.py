from __future__ import annotations

import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from beacon_engine.research.face_check import FaceCheckResult, FaceChecker, classify_face_signal
from beacon_engine.research.providers import ProviderChannel, ProviderVideo
from beacon_engine.research.store import YouTubeStore


class FaceSignalLogicTests(unittest.TestCase):
    def test_verdicts(self) -> None:
        self.assertEqual(classify_face_signal(0, 0, 0)[0], "unknown")
        self.assertEqual(classify_face_signal(10, 10, 8)[0], "on_camera")   # recurring face
        self.assertEqual(classify_face_signal(10, 1, 1)[0], "faceless")     # ~no faces
        self.assertEqual(classify_face_signal(10, 6, 2)[0], "uncertain")    # faces, no recurrence

    def test_confidence_scales_with_sample(self) -> None:
        self.assertLess(classify_face_signal(2, 2, 2)[1], classify_face_signal(8, 8, 8)[1])

    def test_checker_can_use_an_isolated_models_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            models_dir = Path(tmp)
            yunet = models_dir / "face_detection_yunet_2023mar.onnx"
            sface = models_dir / "face_recognition_sface_2021dec.onnx"
            yunet.write_bytes(b"yunet")
            sface.write_bytes(b"sface")
            detector_calls: list[str] = []
            recognizer_calls: list[str] = []
            fake_cv2 = SimpleNamespace(
                FaceDetectorYN=SimpleNamespace(
                    create=lambda path, *_args, **_kwargs: detector_calls.append(path) or object()
                ),
                FaceRecognizerSF=SimpleNamespace(
                    create=lambda path, *_args, **_kwargs: recognizer_calls.append(path) or object()
                ),
            )
            with patch.dict("sys.modules", {"cv2": fake_cv2}):
                checker = FaceChecker(download=False, models_dir=models_dir)

            self.assertEqual(checker.models_dir, models_dir)
            self.assertEqual(detector_calls, [str(yunet)])
            self.assertEqual(recognizer_calls, [str(sface)])


class CombinedFacelessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = YouTubeStore(Path(self.tmp.name) / "i.sqlite3")
        for cid in ("UC_oncam", "UC_noface", "UC_textonly"):
            self.store.upsert_channel(ProviderChannel(
                channel_id=cid, subscribers=5000, view_count=1_000_000, video_count=10))
        # text classifier marked all three faceless
        with self.store.connect() as conn:
            for cid in ("UC_oncam", "UC_noface", "UC_textonly"):
                conn.execute(
                    "INSERT INTO channel_classifications (channel_id, likely_faceless, "
                    "confidence, score, sample_count, positive_title_count, "
                    "negative_title_count, positives_json, negatives_json, classifier, "
                    "classified_at) VALUES (?,1,0.9,3,5,3,0,'[]','[]','text-v3','x')",
                    (cid,))
        # face-check overrides: UC_oncam has a recurring face, UC_noface has none
        self.store.save_face_checks([
            FaceCheckResult("UC_oncam", 10, 10, 9, "on_camera", 1.0),
            FaceCheckResult("UC_noface", 10, 0, 0, "faceless", 1.0),
        ])

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_face_check_overrides_text(self) -> None:
        rows = {r["channel_id"]: r for r in self.store.channel_metrics()}
        self.assertEqual(rows["UC_oncam"]["faceless"], 0)     # on-camera overrides text
        self.assertEqual(rows["UC_noface"]["faceless"], 1)    # confirmed faceless
        self.assertEqual(rows["UC_textonly"]["faceless"], 1)  # no face-check -> text fallback

    def test_faceless_only_filter_uses_combined(self) -> None:
        ids = [r["channel_id"] for r in self.store.channel_metrics(faceless_only=True)]
        self.assertNotIn("UC_oncam", ids)   # excluded by the recurring-face check
        self.assertIn("UC_noface", ids)
        self.assertIn("UC_textonly", ids)

    def test_vlm_verdict_resolves_uncertain(self) -> None:
        self.store.upsert_channel(ProviderChannel(
            channel_id="UC_gray", subscribers=5000, view_count=1_000_000, video_count=10))
        self.store.save_face_checks([FaceCheckResult("UC_gray", 10, 6, 2, "uncertain", 0.5)])
        rows = {r["channel_id"]: r for r in self.store.channel_metrics()}
        self.assertEqual(rows["UC_gray"]["faceless"], 0)  # uncertain + no text signal
        self.store.save_vlm_verdicts([{"channel_id": "UC_gray", "verdict": "faceless"}])
        rows = {r["channel_id"]: r for r in self.store.channel_metrics()}
        self.assertEqual(rows["UC_gray"]["faceless"], 1)  # VLM resolves the gray zone

    def test_vlm_overrides_cpu_facecheck(self) -> None:
        # UC_noface: CPU said faceless; VLM says on_camera -> VLM wins
        self.store.save_vlm_verdicts([{"channel_id": "UC_noface", "verdict": "on_camera"}])
        rows = {r["channel_id"]: r for r in self.store.channel_metrics()}
        self.assertEqual(rows["UC_noface"]["faceless"], 0)

    def test_export_uncertain_for_vlm(self) -> None:
        self.store.upsert_channel(ProviderChannel(
            channel_id="UC_gray2", subscribers=5000, view_count=1_000_000, video_count=10))
        self.store.save_face_checks([FaceCheckResult("UC_gray2", 10, 6, 2, "uncertain", 0.5)])
        for i in range(3):
            self.store.upsert_video(ProviderVideo(
                video_id=f"g{i:08d}", channel_id="UC_gray2", title="t",
                thumbnail_url=f"https://img/{i}.jpg"))
        exp = self.store.export_uncertain_for_vlm()
        entry = {e["channel_id"]: e for e in exp}.get("UC_gray2")
        self.assertIsNotNone(entry)
        self.assertGreaterEqual(len(entry["thumbnails"]), 3)

    def test_channels_for_face_check_needs_enough_thumbnails(self) -> None:
        self.assertEqual(self.store.channels_for_face_check(min_thumbnails=3), [])
        # UC_textonly has no face-check row yet, so it's eligible once it has thumbnails.
        for i in range(4):
            self.store.upsert_video(ProviderVideo(
                video_id=f"vid{i:08d}", channel_id="UC_textonly", title="t",
                thumbnail_url=f"https://img/{i}.jpg"))
        targets = dict(self.store.channels_for_face_check(min_thumbnails=3))
        self.assertIn("UC_textonly", targets)
        self.assertGreaterEqual(len(targets["UC_textonly"]), 3)
        # already-checked channels are skipped (resumable)
        self.assertNotIn("UC_oncam", targets)


if __name__ == "__main__":
    unittest.main()
