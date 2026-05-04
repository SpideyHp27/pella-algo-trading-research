"""Pella shared infrastructure package.

Sharing tier: methodology-shareable (no edge here, just plumbing).

Submodules:
    config   - YAML/JSON config loaders (cached, mtime-aware)
    logger   - JSON-line structured logger (file + console)
    state    - Atomic JSON / JSONL read/write helpers
    clients  - MT5Client, AristhrottleClient, AlertClient

All seven Pella agents (Phase 1+) build on top of this package. The public
surface defined here is a contract; do not break it without coordinating.
"""
