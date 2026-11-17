"""Allow `python3 -m medulla` inside either container runtime."""

from .cli import entry

raise SystemExit(entry())
