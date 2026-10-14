"""Tests for :mod:`demo_pkg.utils`.

Runnable with either ``pytest`` or ``python -m unittest`` from the
``example`` directory.
"""

from __future__ import annotations

import unittest

from demo_pkg.utils import chunk, slugify, word_count


class TestSlugify(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(slugify("Hello, World!"), "hello-world")

    def test_collapses_and_trims_separators(self) -> None:
        self.assertEqual(
            slugify("  Multiple   --- spaces  "), "multiple-spaces"
        )

    def test_empty(self) -> None:
        self.assertEqual(slugify("!!!"), "")


class TestWordCount(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(word_count("one two three"), 3)

    def test_extra_whitespace(self) -> None:
        self.assertEqual(word_count("  a\t b\n c "), 3)

    def test_empty(self) -> None:
        self.assertEqual(word_count(""), 0)


class TestChunk(unittest.TestCase):
    def test_even_split(self) -> None:
        self.assertEqual(list(chunk([1, 2, 3, 4], 2)), [[1, 2], [3, 4]])

    def test_ragged_final_chunk(self) -> None:
        self.assertEqual(
            list(chunk([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]]
        )

    def test_empty_input(self) -> None:
        self.assertEqual(list(chunk([], 3)), [])

    def test_rejects_non_positive_size(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                list(chunk([1, 2, 3], bad))


if __name__ == "__main__":
    unittest.main()
