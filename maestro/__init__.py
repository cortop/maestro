"""maestro — a per-ticket reconciler over append-only event streams.

A project-agnostic successor to wave-based markdown orchestrators. Each ticket is an
independent unit with its own append-only event log; a level-triggered dispatcher
fans out one background reconciler per due ticket. Deterministic plumbing lives in
Python (this package); the intelligence lives in Claude sessions that drive it
through the ``maestro`` CLI.
"""
__version__ = "0.1.0"
