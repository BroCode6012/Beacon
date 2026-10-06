"""Protocol-level guards for UNTRUSTED bundle files: the compressed-size cap and the
decompression-bomb (bounded-read) cap in load_bundle. These run BEFORE the HMAC check, so
a hostile node could feed a giant file — the caps must reject it before it hits memory."""
from __future__ import annotations

import gzip

import pytest

from beacon_engine.edge.protocol import EdgeProtocolError, load_bundle


def test_load_bundle_rejects_oversized_compressed_file(tmp_path):
    path = tmp_path / "big.signal-edge.json.gz"
    path.write_bytes(gzip.compress(b'{"x":1}' * 4000))
    with pytest.raises(EdgeProtocolError, match="compressed bundle exceeds"):
        load_bundle(path, signing_key="k", max_uncompressed_bytes=20)


def test_load_bundle_rejects_decompression_bomb(tmp_path):
    # ~2 MB of one repeated byte gzips to a few KB: it slips under the compressed-size cap but
    # must be rejected on the bounded read before it can inflate into memory.
    path = tmp_path / "bomb.signal-edge.json.gz"
    path.write_bytes(gzip.compress(b'{"a":"' + b"A" * 2_000_000 + b'"}'))
    with pytest.raises(EdgeProtocolError, match="uncompressed bundle exceeds"):
        load_bundle(path, signing_key="k", max_uncompressed_bytes=100_000)
