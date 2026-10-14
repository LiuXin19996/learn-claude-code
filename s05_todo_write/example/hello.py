"""Minimal greeting example.

This module demonstrates a small, well-structured Python entry point:
it exposes a reusable :func:`greet` helper and runs it from a
``main`` function guarded by the standard ``if __name__ == "__main__"``
block, so importing the module has no side effects.
"""

from __future__ import annotations


def greet(name: str) -> str:
    """Build and print a greeting for ``name``.

    Args:
        name: The name of the person (or thing) to greet.

    Returns:
        The greeting message that was printed.
    """
    message = f"Hello, {name}"
    print(message)
    return message


def main() -> None:
    """Run the example greeting."""
    greet("Claude")


if __name__ == "__main__":
    main()
