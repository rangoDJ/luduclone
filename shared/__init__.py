"""Shared core for luduclone: manifest parsing, placeholder resolution, scanning.

Used by both the server (to serve/validate the manifest) and the clients
(to resolve save locations on Windows and Linux).
"""

__all__ = ["manifest", "placeholders", "scan"]
