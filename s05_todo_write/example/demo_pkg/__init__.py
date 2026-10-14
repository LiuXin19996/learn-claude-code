"""demo_pkg - a small example package.

This package bundles a few pure utility helpers together with their
tests. It is intentionally tiny so it can serve as a template for
larger packages in this course.

Example:
    >>> from demo_pkg import slugify
    >>> slugify("Hello, World!")
    'hello-world'
"""

from __future__ import annotations

from .utils import chunk, slugify, word_count

__all__ = ["chunk", "slugify", "word_count"]

__version__ = "0.1.0"
