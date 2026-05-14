"""
language_adapter.py — Plugin interface for language-specific extraction.

WHY a plugin interface:
- V1 targets Java only.
- V2 will add Python, JavaScript, TypeScript.
- With this interface, adding a new language = adding one adapter class.
  Core extractor code never changes.

HOW to add a new language (V2):
1. Create class PythonAdapter(LanguageAdapter)
2. Set extensions = [".py"]
3. Register in ADAPTERS dict below
4. Add "py" to config.yaml target_extensions
"""

from abc import ABC, abstractmethod
from typing import List


class LanguageAdapter(ABC):
    """
    Base class for language-specific extraction logic.
    All methods have safe defaults — subclasses override only what they need.
    """

    @property
    @abstractmethod
    def extensions(self) -> List[str]:
        """File extensions this adapter handles. Example: ['.java']"""

    def is_target_file(self, file_path: str) -> bool:
        """Return True if this file should be processed."""
        return any(file_path.endswith(ext) for ext in self.extensions)

    def decode_content(self, raw_bytes: bytes) -> str:
        """
        Decode file bytes to string.
        Uses UTF-8 with replacement for malformed bytes —
        real-world Java files sometimes have Latin-1 in comments.
        """
        try:
            return raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return raw_bytes.decode("latin-1", errors="replace")

    def count_tokens(self, code: str) -> int:
        """
        Rough token count for noise filtering.
        We split on whitespace + common delimiters.
        Fast and good enough for a min-token filter.
        No need for a real tokenizer here.
        """
        import re
        return len(re.findall(r'\w+', code))


class JavaAdapter(LanguageAdapter):
    """Java language adapter — V1 target language."""

    @property
    def extensions(self) -> List[str]:
        return [".java"]


class PythonAdapter(LanguageAdapter):
    """Python language adapter — V2 placeholder."""

    @property
    def extensions(self) -> List[str]:
        return [".py"]


class JavaScriptAdapter(LanguageAdapter):
    """JavaScript language adapter — V2 placeholder."""

    @property
    def extensions(self) -> List[str]:
        return [".js"]


class TypeScriptAdapter(LanguageAdapter):
    """TypeScript language adapter — V2 placeholder."""

    @property
    def extensions(self) -> List[str]:
        return [".ts"]


# Registry — maps file extension to adapter instance.
# Config loader uses target_extensions list to determine
# which adapters are active for a given pipeline run.
ADAPTERS: dict = {
    ".java": JavaAdapter(),
    ".py": PythonAdapter(),
    ".js": JavaScriptAdapter(),
    ".ts": TypeScriptAdapter(),
}


def get_adapter(extension: str) -> LanguageAdapter:
    """
    Return adapter for a given extension.
    Raises KeyError if extension is not registered.
    """
    ext = extension.lower()
    if ext not in ADAPTERS:
        raise KeyError(
            f"No adapter registered for '{ext}'. "
            f"Available: {list(ADAPTERS.keys())}"
        )
    return ADAPTERS[ext]