"""Pure utility helpers for :mod:`demo_pkg`.

Every function here is side-effect free and easy to test: given the
same inputs they always return the same outputs.
"""

from __future__ import annotations

import re
from typing import Iterator, Sequence, TypeVar

T = TypeVar("T")

# One or more characters that are not letters/digits, collapsed to a hyphen.
_NON_WORD = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """Convert ``text`` into a lowercase, hyphen-separated slug.

    Args:
        text: Arbitrary input text.

    Returns:
        A URL-friendly slug with no leading/trailing hyphens.

    Example:
        >>> slugify("Hello, World!")
        'hello-world'
    """
    return _NON_WORD.sub("-", text.lower()).strip("-")


def word_count(text: str) -> int:
    """Count whitespace-separated words in ``text``.

    Args:
        text: Arbitrary input text.

    Returns:
        The number of words; ``0`` for an empty or whitespace-only string.
    """
    return len(text.split())


def chunk(items: Sequence[T], size: int) -> Iterator[Sequence[T]]:
    """Split ``items`` into consecutive chunks of length ``size``.

    Args:
        items: Any sliceable sequence.
        size: Maximum length of each chunk. Must be positive.

    Yields:
        Successive slices of ``items``; the final chunk may be shorter.

    Raises:
        ValueError: If ``size`` is not a positive integer.

    Example:
        >>> list(chunk([1, 2, 3, 4, 5], 2))
        [[1, 2], [3, 4], [5]]
    """
    if size <= 0:
        raise ValueError("size must be a positive integer")
    for start in range(0, len(items), size):
        end = start + size
        yield items[start:end]
