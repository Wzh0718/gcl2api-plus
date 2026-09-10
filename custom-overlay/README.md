# Custom Overlay

`files/` mirrors paths from the upstream repository. The release script clones the latest upstream `master`, then copies this directory over the clone before testing and building.

Rules:

- Keep only customized source, UI, configuration examples, Docker exclusions, and focused tests here.
- A file in this directory replaces the complete upstream file at the same path.
- Never add `.env`, credentials, SQLite databases, Git metadata, or registry secrets.
- When a customized working-tree file changes, update its matching copy under `files/`; `tests/test_build_and_push_script.py` detects drift.
