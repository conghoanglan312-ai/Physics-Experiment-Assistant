"""Validation for text sent to the language model."""

from __future__ import annotations

import re
import unicodedata

from llm.config.settings import MAX_USER_INPUT_LENGTH


class UserQueryLimiter:
    """Defense-in-depth validation for one user query."""

    _CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
    _PROMPT_INJECTION_PATTERNS = (
        re.compile(r"\bignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|rules|prompts?)\b", re.I),
        re.compile(r"\b(?:reveal|show|print|repeat|dump)\s+(?:the\s+)?(?:system\s+)?(?:prompt|instructions?)\b", re.I),
        re.compile(r"\b(?:act as|you are now)\s+(?:a\s+)?(?:system|developer|jailbreak)\b", re.I),
        re.compile(r"\b(?:developer|system)\s+(?:message|prompt|instructions?)\s*:", re.I),
        re.compile(r"\bdo anything now\b|\bjailbreak\b", re.I),
    )

    def __init__(self, user_query: str):
        self.user_query = user_query

    def validate(self) -> tuple[bool, str | None]:
        """Return whether the query is safe to submit and an error if it is not."""
        if not isinstance(self.user_query, str):
            return False, "Your question must be text."

        normalized_query = unicodedata.normalize("NFKC", self.user_query).strip()
        if not normalized_query:
            return False, "Please enter a question."
        if len(normalized_query) > MAX_USER_INPUT_LENGTH:
            return False, f"Please keep your question to {MAX_USER_INPUT_LENGTH} characters or fewer."
        if self._CONTROL_CHARACTERS.search(normalized_query):
            return False, "Your question contains unsupported control characters."
        if any(pattern.search(normalized_query) for pattern in self._PROMPT_INJECTION_PATTERNS):
            return False, "I can help with Millikan oil-drop experiment questions, but not with changing or revealing assistant instructions."

        return True, None

    def is_input_length_valid(self) -> bool:
        return isinstance(self.user_query, str) and len(
            unicodedata.normalize("NFKC", self.user_query).strip()
        ) <= MAX_USER_INPUT_LENGTH

    def isInputLengthValid(self) -> bool:
        """Compatibility wrapper for the original public method name."""
        return self.is_input_length_valid()


# Compatibility alias for existing callers using the original class name.
userQueryLimiter = UserQueryLimiter
