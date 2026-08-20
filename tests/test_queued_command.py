"""Regression tests for queued_command attachment handling.

Claude Code logs three shapes under attachment.type == "queued_command":
  1. a prompt the human typed mid-turn            -> index as user
  2. the same, with image blocks (prompt is list) -> index text as user
  3. a background task notification               -> skip
Shapes (1) and (3) share the record type; only commandMode/origin differ.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import scope_indexer  # noqa: E402


def _att(prompt, command_mode="prompt", origin={"kind": "human"}):
    att = {
        "type": "queued_command",
        "prompt": prompt,
        "commandMode": command_mode,
        "timestamp": "2026-08-20T00:00:00.000Z",
    }
    if origin is not None:
        att["origin"] = origin
    return {"type": "attachment", "attachment": att}


class QueuedCommandTests(unittest.TestCase):
    def test_human_string_prompt_is_indexed_as_user(self):
        obj = _att("where do i roll the api key")
        self.assertEqual(
            scope_indexer._extract_message(obj),
            ("user", "where do i roll the api key"),
        )

    def test_human_prompt_with_image_blocks_indexes_text(self):
        obj = _att([
            {"type": "text", "text": "this should say Book Dash"},
            {"type": "image", "source": {"type": "base64", "data": "AAAA"}},
        ])
        self.assertEqual(
            scope_indexer._extract_message(obj),
            ("user", "this should say Book Dash"),
        )

    def test_task_notification_is_skipped(self):
        obj = _att(
            "<task-notification>\n<task-id>bm8fd4uki</task-id>\n"
            "<status>completed</status>\n</task-notification>",
            command_mode="task-notification",
            origin=None,
        )
        self.assertIsNone(scope_indexer._extract_message(obj))

    def test_xml_shaped_prompt_is_skipped_even_if_marked_human(self):
        # Mirrors the type:"user" path, which drops injected "<...>" payloads.
        obj = _att("<system-reminder>noise</system-reminder>")
        self.assertIsNone(scope_indexer._extract_message(obj))

    def test_non_human_origin_is_skipped(self):
        obj = _att("hello", origin={"kind": "agent"})
        self.assertIsNone(scope_indexer._extract_message(obj))


if __name__ == "__main__":
    unittest.main()
