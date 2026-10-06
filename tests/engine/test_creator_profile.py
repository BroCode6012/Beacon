"""Global creator profile: the one instruction card every AI call carries."""

from __future__ import annotations

import os
import tempfile
import unittest


class _Cwd:
    def __init__(self) -> None:
        self._old = os.getcwd()
        self._tmp = tempfile.TemporaryDirectory()

    def __enter__(self):
        os.chdir(self._tmp.name)
        return self._tmp.name

    def __exit__(self, *exc):
        os.chdir(self._old)
        self._tmp.cleanup()


class CreatorProfileTests(unittest.TestCase):
    def test_default_teaches_faceless(self) -> None:
        from beacon_engine.creator_profile import DEFAULT_PROFILE
        self.assertIn("FACELESS", DEFAULT_PROFILE)
        self.assertIn("FORMAT", DEFAULT_PROFILE)   # format = thumbnails + presentation

    def test_save_and_load_roundtrip(self) -> None:
        from beacon_engine.creator_profile import load_profile, save_profile
        with _Cwd():
            save_profile("My rule: only faceless references, ever.")
            self.assertEqual(load_profile(), "My rule: only faceless references, ever.")
            save_profile("")   # emptying falls back to the default card
            self.assertIn("FACELESS", load_profile())

    def test_compose_prepends_profile_to_instructions(self) -> None:
        from beacon_engine.creator_profile import compose_instructions
        with _Cwd():
            merged = compose_instructions("You are a title writer.")
            self.assertIn("CREATOR PROFILE", merged)
            self.assertIn("FACELESS", merged)
            self.assertTrue(merged.endswith("You are a title writer."))


if __name__ == "__main__":
    unittest.main()
