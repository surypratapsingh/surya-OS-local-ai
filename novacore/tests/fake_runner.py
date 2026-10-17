"""Small fake model runner script for novacore CLI socket tests."""
from __future__ import annotations

import sys


def main() -> None:
    # Drain stdin if data is provided
    try:
        if not sys.stdin.isatty():
            sys.stdin.read()
    except Exception:
        pass
    print("I am NovaCore, running strictly local via CLI.")


if __name__ == "__main__":
    main()
