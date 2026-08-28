from __future__ import annotations

import unittest

from lkt.web import chat_messages


class WebInputTests(unittest.TestCase):
    def test_chat_messages_keep_only_bounded_user_and_assistant_history(self) -> None:
        messages = chat_messages(
            {
                "message": "new question",
                "history": [
                    {"role": "system", "content": "discard me"},
                    {"role": "user", "content": "earlier"},
                    {"role": "assistant", "content": "earlier reply"},
                    {"role": "invalid", "content": "discard me too"},
                ],
            }
        )
        self.assertEqual(
            messages,
            [
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "earlier reply"},
                {"role": "user", "content": "new question"},
            ],
        )

    def test_chat_rejects_an_empty_message(self) -> None:
        with self.assertRaises(ValueError):
            chat_messages({"message": "  "})


if __name__ == "__main__":
    unittest.main()
