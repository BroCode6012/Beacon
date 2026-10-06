"""Free, CPU face-check: confirm faceless vs on-camera from channel thumbnails.

Uses OpenCV's tiny YuNet face detector + SFace face-recognition embeddings
(both free, CPU-fast, ~38MB total) to ask the one question that actually
separates faceless from on-camera: does the SAME human face recur across a
channel's thumbnails?

Produces a 3-way signal so we never overstate certainty:
* ``on_camera`` -- a single face recurs across thumbnails (a presenter).
* ``faceless``  -- almost no faces appear at all (animation/stock/text).
* ``uncertain`` -- faces appear but no single recurring person (documentary /
  anthology gray zone) -> let the text classifier or a VLM decide.

Confidence scales with how many thumbnails we had (4 is too few to trust).
"""

from __future__ import annotations

import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
import requests

from beacon_engine.logging_util import get_logger

log = get_logger()
Progress = Callable[[str], None]

MODELS_DIR = Path("storage") / "models"
YUNET_PATH = MODELS_DIR / "face_detection_yunet_2023mar.onnx"
SFACE_PATH = MODELS_DIR / "face_recognition_sface_2021dec.onnx"
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")
SFACE_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_recognition_sface/face_recognition_sface_2021dec.onnx")

# Decision thresholds (validated against known channels).
RECURRING_ON_CAMERA = 0.5    # >=50% of thumbnails share one face -> single presenter
FACELESS_FACE_RATIO = 0.15   # <=15% of thumbnails have any face -> no people
SAME_FACE_COSINE = 0.363     # SFace cosine threshold for "same identity"
RELIABLE_SAMPLE = 8          # thumbnails needed for full confidence


class FaceCheckUnavailable(RuntimeError):
    """OpenCV or the face models are not available."""


@dataclass
class FaceCheckResult:
    channel_id: str
    thumbnails_checked: int = 0
    faces_found: int = 0
    recurring_count: int = 0
    verdict: str = "unknown"
    confidence: float = 0.0

    @property
    def face_ratio(self) -> float:
        return self.faces_found / self.thumbnails_checked if self.thumbnails_checked else 0.0

    @property
    def recurring_ratio(self) -> float:
        return self.recurring_count / self.thumbnails_checked if self.thumbnails_checked else 0.0


def classify_face_signal(
    thumbnails_checked: int,
    faces_found: int,
    recurring_count: int,
) -> tuple[str, float]:
    """Pure verdict logic (no models) -- unit-testable."""
    if thumbnails_checked <= 0:
        return "unknown", 0.0
    face_ratio = faces_found / thumbnails_checked
    recurring_ratio = recurring_count / thumbnails_checked
    confidence = round(min(1.0, thumbnails_checked / RELIABLE_SAMPLE), 2)
    if recurring_ratio >= RECURRING_ON_CAMERA:
        return "on_camera", confidence
    if face_ratio <= FACELESS_FACE_RATIO:
        return "faceless", confidence
    return "uncertain", round(confidence * 0.5, 2)


class FaceChecker:
    """Loads the (free) OpenCV face models once and checks channels."""

    def __init__(
        self,
        *,
        download: bool = True,
        models_dir: str | Path | None = None,
    ) -> None:
        try:
            import cv2  # noqa: F401
        except Exception as exc:  # noqa: BLE001
            raise FaceCheckUnavailable(
                "opencv-python-headless is not installed (pip install opencv-python-headless)"
            ) from exc
        self._cv2 = cv2
        self.models_dir = Path(models_dir) if models_dir is not None else MODELS_DIR
        self.yunet_path = self.models_dir / YUNET_PATH.name
        self.sface_path = self.models_dir / SFACE_PATH.name
        self.models_dir.mkdir(parents=True, exist_ok=True)
        if download:
            self._ensure_model(self.yunet_path, YUNET_URL)
            self._ensure_model(self.sface_path, SFACE_URL)
        if not (self.yunet_path.exists() and self.sface_path.exists()):
            raise FaceCheckUnavailable("face models missing and could not be downloaded")
        self._det = cv2.FaceDetectorYN.create(
            str(self.yunet_path),
            "",
            (320, 320),
            score_threshold=0.6,
        )
        self._rec = cv2.FaceRecognizerSF.create(str(self.sface_path), "")
        self._session = requests.Session()

    @staticmethod
    def _ensure_model(path: Path, url: str) -> None:
        if path.exists() and path.stat().st_size > 0:
            return
        try:
            urllib.request.urlretrieve(url, path)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not download face model %s: %s", path.name, exc)

    def _embed_biggest_face(self, img):
        h, w = img.shape[:2]
        self._det.setInputSize((w, h))
        _, faces = self._det.detect(img)
        if faces is None or len(faces) == 0:
            return None
        biggest = max(faces, key=lambda r: float(r[2]) * float(r[3]))
        return self._rec.feature(self._rec.alignCrop(img, biggest))

    def check_channel(
        self,
        channel_id: str,
        thumbnail_urls: Iterable[str],
        *,
        max_thumbnails: int = 12,
    ) -> FaceCheckResult:
        cv2 = self._cv2
        feats: list = []
        checked = 0
        for url in list(dict.fromkeys(u for u in thumbnail_urls if u))[:max_thumbnails]:
            try:
                data = self._session.get(url, timeout=12).content
                img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
                if img is None:
                    continue
                checked += 1
                feat = self._embed_biggest_face(img)
                if feat is not None:
                    feats.append(feat)
            except Exception:  # noqa: BLE001
                continue
        recurring = 0
        for i in range(len(feats)):
            group = sum(
                1 for j in range(len(feats))
                if self._rec.match(feats[i], feats[j], cv2.FaceRecognizerSF_FR_COSINE)
                > SAME_FACE_COSINE
            )
            recurring = max(recurring, group)
        verdict, confidence = classify_face_signal(checked, len(feats), recurring)
        return FaceCheckResult(channel_id, checked, len(feats), recurring, verdict, confidence)


def run_face_check(
    store,
    *,
    checker: FaceChecker | None = None,
    min_thumbnails: int = 3,
    limit: int | None = None,
    recheck: bool = False,
    min_subs: int = 0,
    on_step: Progress = lambda _m: None,
) -> dict:
    """Face-check channels that have enough thumbnails; save 3-way verdicts."""
    checker = checker or FaceChecker()
    targets = store.channels_for_face_check(
        min_thumbnails=min_thumbnails, limit=limit, recheck=recheck, min_subs=min_subs)
    on_step(f"{len(targets):,} channels have >={min_thumbnails} thumbnails to check")
    counts = {"on_camera": 0, "faceless": 0, "uncertain": 0, "unknown": 0}
    batch: list[FaceCheckResult] = []
    for idx, (channel_id, urls) in enumerate(targets, 1):
        result = checker.check_channel(channel_id, urls)
        counts[result.verdict] = counts.get(result.verdict, 0) + 1
        batch.append(result)
        if len(batch) >= 50:
            store.save_face_checks(batch)
            batch = []
            on_step(
                f"{idx}/{len(targets)} checked  "
                f"(on-camera {counts['on_camera']:,} / faceless {counts['faceless']:,} "
                f"/ uncertain {counts['uncertain']:,})"
            )
    if batch:
        store.save_face_checks(batch)
    counts["total"] = len(targets)
    return counts


def run_face_check_parallel(
    store,
    *,
    workers: int = 8,
    min_thumbnails: int = 3,
    limit: int | None = None,
    recheck: bool = False,
    min_subs: int = 0,
    on_step: Progress = lambda _m: None,
) -> dict:
    """Same as run_face_check but downloads/scores channels in parallel.

    The slow part is downloading thumbnails; a thread-local FaceChecker per
    worker parallelizes both the network I/O and the CV detection (cv2 objects
    are not shared across threads)."""
    import threading
    from concurrent.futures import ThreadPoolExecutor, as_completed

    targets = store.channels_for_face_check(
        min_thumbnails=min_thumbnails, limit=limit, recheck=recheck, min_subs=min_subs)
    on_step(f"{len(targets):,} channels have >={min_thumbnails} thumbnails to check "
            f"({workers} parallel workers)")

    _local = threading.local()

    def _checker() -> FaceChecker:
        c = getattr(_local, "checker", None)
        if c is None:
            c = FaceChecker()
            _local.checker = c
        return c

    counts = {"on_camera": 0, "faceless": 0, "uncertain": 0, "unknown": 0}
    lock = threading.Lock()
    batch: list[FaceCheckResult] = []
    done = 0

    def work(item) -> FaceCheckResult | None:
        channel_id, urls = item
        try:
            return _checker().check_channel(channel_id, urls)
        except Exception:  # noqa: BLE001 -- one bad channel can't sink the batch
            return None

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(work, item) for item in targets]
        for fut in as_completed(futures):
            result = fut.result()
            if result is None:
                continue
            with lock:
                counts[result.verdict] = counts.get(result.verdict, 0) + 1
                batch.append(result)
                done += 1
                if len(batch) >= 50:
                    store.save_face_checks(batch)
                    batch = []
                    on_step(
                        f"{done}/{len(targets)} checked  "
                        f"(on-camera {counts['on_camera']:,} / faceless {counts['faceless']:,} "
                        f"/ uncertain {counts['uncertain']:,})"
                    )
    if batch:
        store.save_face_checks(batch)
    counts["total"] = len(targets)
    return counts
