from __future__ import annotations

import dataclasses
import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
from datetime import datetime, timezone


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from vibe_memory_events import EVENTS, NormalizedEvent, normalize_event


FIXTURES = ROOT / "tests/fixtures/hooks"


class NormalizeEventTest(unittest.TestCase):
    def test_supported_events_are_the_shared_five_event_contract(self) -> None:
        self.assertEqual(
            EVENTS,
            ("SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact", "Stop"),
        )

    def test_normalized_event_has_only_safe_contract_fields(self) -> None:
        self.assertEqual(
            list(NormalizedEvent.__dataclass_fields__),
            ["agent", "event", "cwd", "session_id", "timestamp", "payload_digest"],
        )
        self.assertTrue(NormalizedEvent.__dataclass_params__.frozen)

    def test_normalizes_codex_fixture_without_retaining_prompt(self) -> None:
        payload = self._fixture("codex_user_prompt.json")

        normalized = normalize_event("codex", "UserPromptSubmit", payload, ROOT)

        self.assertEqual(normalized.agent, "codex")
        self.assertEqual(normalized.event, "UserPromptSubmit")
        self.assertEqual(normalized.cwd, ROOT.resolve())
        self.assertEqual(normalized.session_id, "codex-session-42")
        self._assert_timestamp_is_local_and_second_precision(normalized.timestamp)
        self.assertEqual(normalized.payload_digest, self._digest(payload))
        self.assertNotIn(payload["prompt"], repr(dataclasses.asdict(normalized)))

    def test_normalizes_claude_fixture_session_id(self) -> None:
        payload = self._fixture("claude_user_prompt.json")

        normalized = normalize_event("claude-code", "UserPromptSubmit", payload, ROOT)

        self.assertEqual(normalized.agent, "claude-code")
        self.assertEqual(normalized.session_id, "claude-session-17")
        self.assertEqual(normalized.cwd, ROOT.resolve())

    def test_uses_fallback_cwd_and_unknown_session_for_missing_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            fallback = pathlib.Path(value) / "fallback"
            fallback.mkdir()

            normalized = normalize_event(
                "codex", "UserPromptSubmit", "not metadata", fallback
            )

            self.assertEqual(normalized.cwd, fallback.resolve())
            self.assertEqual(normalized.session_id, "unknown")

    def test_canonicalizes_cwd(self) -> None:
        with tempfile.TemporaryDirectory() as value:
            root = pathlib.Path(value)
            actual = root / "actual"
            actual.mkdir()
            link = root / "link"
            try:
                link.symlink_to(actual, target_is_directory=True)
            except (NotImplementedError, OSError) as error:
                self.skipTest(f"symlinks unavailable: {error}")

            normalized = normalize_event(
                "codex", "UserPromptSubmit", {"cwd": str(link)}, root
            )

            self.assertEqual(normalized.cwd, actual.resolve())

    def test_digest_is_deterministic_regardless_of_dictionary_key_order(self) -> None:
        first = {"session_id": "same", "prompt": "harmless", "nested": {"b": 2, "a": 1}}
        second = {"nested": {"a": 1, "b": 2}, "prompt": "harmless", "session_id": "same"}

        first_normalized = normalize_event("codex", "UserPromptSubmit", first, ROOT)
        second_normalized = normalize_event("codex", "UserPromptSubmit", second, ROOT)

        self.assertEqual(first_normalized.payload_digest, second_normalized.payload_digest)

    def test_rejects_unsupported_agent(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported agent"):
            normalize_event("other", "UserPromptSubmit", {}, ROOT)

    def test_rejects_unsupported_event(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported event"):
            normalize_event("codex", "UnknownEvent", {}, ROOT)

    def test_rejects_unserializable_payload_without_repr_leakage(self) -> None:
        class SecretValue:
            def __repr__(self) -> str:
                return "SECRET-SHOULD-NOT-LEAK"

        with self.assertRaisesRegex(ValueError, "JSON-serializable") as error:
            normalize_event(
                "codex", "UserPromptSubmit", {"secret": SecretValue()}, ROOT
            )

        self.assertNotIn("SECRET-SHOULD-NOT-LEAK", str(error.exception))

    def test_timestamp_assertion_rejects_utc_timestamp_in_non_utc_local_timezone(self) -> None:
        local_offset = datetime.now().astimezone().utcoffset()
        if local_offset == timezone.utc.utcoffset(None):
            self.skipTest("local timezone is UTC")

        utc_timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

        with self.assertRaisesRegex(AssertionError, "local UTC offset"):
            self._assert_timestamp_is_local_and_second_precision(utc_timestamp)

    @staticmethod
    def _fixture(name: str) -> dict[str, object]:
        return json.loads((FIXTURES / name).read_text(encoding="utf-8"))

    @staticmethod
    def _digest(payload: object) -> str:
        canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _assert_timestamp_is_local_and_second_precision(self, value: str) -> None:
        timestamp = datetime.fromisoformat(value)
        self.assertIsNotNone(timestamp.tzinfo)
        self.assertEqual(
            timestamp.utcoffset(),
            datetime.now().astimezone().utcoffset(),
            "local UTC offset",
        )
        self.assertEqual(timestamp.microsecond, 0)


if __name__ == "__main__":
    unittest.main()
