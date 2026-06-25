#!/usr/bin/env python
"""Entry point for the packaged luduclone GUI executable (PyInstaller target).

Equivalent to ``python -m client.gui``; bundled into ``luduclone-gui.exe`` by CI.
"""
import sys

from client.gui import main

if __name__ == "__main__":
    sys.exit(main())
