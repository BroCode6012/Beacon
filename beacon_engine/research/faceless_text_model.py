"""Fast, vision-free faceless classifier.

A TF-IDF + logistic-regression model over each channel's TEXT (channel title +
channel description + its top video titles), trained on the labels the vision
layers already produced (channel_face_check + channel_vlm_check). It predicts
faceless vs on-camera in milliseconds over the whole index, with NO downloads,
NO thumbnails, NO GPU — so it can label every channel (and every newly-discovered
one) instantly. It is used as the verdict FALLBACK in store.channel_metrics
(below the accurate vision checks, above the old keyword classifier).

Train once, reuse the persisted model to score cheaply. Retrain periodically as
the vision layers add more labels.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable

from beacon_engine.logging_util import get_logger
from beacon_engine.research.store import YouTubeStore, utc_now_iso

log = get_logger()
Progress = Callable[[str], None]

MODEL_VERSION = "tfidf-logreg-v1"
MODEL_FILENAME = "faceless_text_model.joblib"
TOP_VIDEO_TITLES = 12
THRESHOLD = 0.5          # P(faceless) >= THRESHOLD -> faceless


def _model_path(store: YouTubeStore) -> Path:
    return store.state_directory / MODEL_FILENAME


def _labels(conn) -> dict[str, int]:
    """channel_id -> 1 (faceless) / 0 (on_camera), from the vision layers.
    VLM (the gray-zone resolver) overrides the CPU face-check where both exist."""
    labels: dict[str, int] = {}
    for cid, v in conn.execute(
            "SELECT channel_id, verdict FROM channel_face_check "
            "WHERE verdict IN ('faceless','on_camera')"):
        labels[cid] = 1 if v == "faceless" else 0
    for cid, v in conn.execute(
            "SELECT channel_id, verdict FROM channel_vlm_check "
            "WHERE verdict IN ('faceless','on_camera')"):
        labels[cid] = 1 if v == "faceless" else 0
    return labels


def _top_titles(conn) -> dict[str, list[str]]:
    """channel_id -> its top-N video titles by views (one indexed scan)."""
    out: dict[str, list[str]] = defaultdict(list)
    rows = conn.execute(
        f"""
        SELECT channel_id, title FROM (
            SELECT channel_id, title,
                   ROW_NUMBER() OVER (PARTITION BY channel_id
                                      ORDER BY COALESCE(views,0) DESC) AS rn
            FROM videos WHERE COALESCE(title,'') != ''
        ) WHERE rn <= {int(TOP_VIDEO_TITLES)}
        """)
    for cid, title in rows:
        out[cid].append(title)
    return out


def _blob(title: str, desc: str, titles: list[str]) -> str:
    return " \n ".join(p for p in ([title or "", desc or ""] + list(titles)) if p).strip()


def _build_blobs(conn, titles_by_channel: dict[str, list[str]]) -> dict[str, str]:
    blobs: dict[str, str] = {}
    for cid, title, desc in conn.execute(
            "SELECT channel_id, COALESCE(title,''), COALESCE(description,'') FROM channels"):
        b = _blob(title, desc, titles_by_channel.get(cid, []))
        if b:
            blobs[cid] = b
    return blobs


def _new_pipeline():
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    return Pipeline([
        ("tfidf", TfidfVectorizer(max_features=30000, ngram_range=(1, 2), min_df=2,
                                  sublinear_tf=True, strip_accents="unicode", lowercase=True)),
        ("clf", LogisticRegression(max_iter=1000, class_weight="balanced", C=4.0)),
    ])


def train(store: YouTubeStore | None = None, *, on_step: Progress = lambda _m: None) -> dict:
    """Train on the vision labels, report held-out accuracy, persist the model."""
    import joblib
    from sklearn.model_selection import train_test_split
    from sklearn.metrics import accuracy_score

    store = store or YouTubeStore()
    with store.connect() as conn:
        labels = _labels(conn)
        titles = _top_titles(conn)
        blobs = _build_blobs(conn, titles)

    X, y = [], []
    for cid, lab in labels.items():
        b = blobs.get(cid)
        if b:
            X.append(b); y.append(lab)
    if len(set(y)) < 2 or len(y) < 200:
        raise RuntimeError(f"not enough labeled data to train (n={len(y)})")

    on_step(f"Training faceless text model on {len(y):,} labeled channels…")
    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, stratify=y, random_state=42)
    holdout = _new_pipeline(); holdout.fit(Xtr, ytr)
    acc = float(accuracy_score(yte, holdout.predict(Xte)))

    final = _new_pipeline(); final.fit(X, y)          # refit on ALL labels for the shipped model
    path = _model_path(store)
    joblib.dump({"pipe": final, "version": MODEL_VERSION}, path)
    metrics = {"samples": len(y), "faceless": int(sum(y)),
               "on_camera": int(len(y) - sum(y)), "holdout_accuracy": round(acc, 4),
               "model_path": str(path)}
    on_step(f"Trained (held-out accuracy {acc:.3f}); saved -> {path.name}")
    return metrics


def _load(store: YouTubeStore):
    import joblib
    path = _model_path(store)
    if not path.exists():
        return None
    try:
        return joblib.load(path).get("pipe")
    except Exception:  # noqa: BLE001
        return None


def score(store: YouTubeStore | None = None, *, only_missing: bool = False,
          on_step: Progress = lambda _m: None) -> int:
    """Score channels with the persisted model and write channel_text_model.
    only_missing=True scores just channels without a prediction yet (radar hook)."""
    store = store or YouTubeStore()
    pipe = _load(store)
    if pipe is None:
        on_step("Faceless text model not trained yet — skipping scoring")
        return 0
    with store.connect() as conn:
        titles = _top_titles(conn)
        blobs = _build_blobs(conn, titles)
        if only_missing:
            have = {r[0] for r in conn.execute("SELECT channel_id FROM channel_text_model")}
            blobs = {cid: b for cid, b in blobs.items() if cid not in have}
        if not blobs:
            return 0
        ids = list(blobs)
        probs = pipe.predict_proba([blobs[c] for c in ids])[:, 1]
        now = utc_now_iso()
        rows = [(cid, int(p >= THRESHOLD), float(p), MODEL_VERSION, now)
                for cid, p in zip(ids, probs)]
        conn.executemany(
            """
            INSERT INTO channel_text_model (channel_id, faceless, proba, model_version, scored_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                faceless=excluded.faceless, proba=excluded.proba,
                model_version=excluded.model_version, scored_at=excluded.scored_at
            """, rows)
    on_step(f"Scored {len(rows):,} channels with the faceless text model")
    return len(rows)


def train_and_score(store: YouTubeStore | None = None, *,
                    on_step: Progress = lambda _m: None) -> dict:
    store = store or YouTubeStore()
    metrics = train(store, on_step=on_step)
    metrics["scored"] = score(store, on_step=on_step)
    return metrics
