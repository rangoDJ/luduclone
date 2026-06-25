#!/usr/bin/env python
"""Entry point for the packaged luduclone client executable (PyInstaller target).

Equivalent to ``python -m client``; bundled into ``luduclone.exe`` by CI.
"""
import sys

from client.cli import main

if __name__ == "__main__":
    sys.exit(main())
