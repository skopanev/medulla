"""Allow `python3 -m medulla` (used by docker.py inside the container)."""

from .cli import entry

raise SystemExit(entry())
