"""Entry point for the `mnemos-train` console script (ADR-0021 NM track).

`training/` is a repo-local top-level package, deliberately excluded from the
wheel (training happens outside the server runtime — ADR-0021 anti-scope).
This wrapper resolves the repo-local module when it exists (editable/dev
installs, toolbox containers with the repo mounted) and fails loud with
actionable guidance when it does not (plain wheel installs of the server).
"""

from __future__ import annotations

import sys
from pathlib import Path

_FAIL_MSG = (
    "mnemos-train is not available in this installation: the training package "
    "lives in the repository (training/) and is excluded from the server wheel "
    "by design (ADR-0021). Run from a repository checkout "
    "(pip install -e '.[training]' or a mounted toolbox container), "
    "or call `python training/train.py` directly."
)


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent.parent
    train_dir = repo_root / "training"
    if not (train_dir / "train.py").exists():
        print(_FAIL_MSG, file=sys.stderr)
        return 3
    # Resolve training/ as an importable package regardless of cwd.
    repo_str = str(repo_root)
    if repo_str not in sys.path:
        sys.path.insert(0, repo_str)
    try:
        from training.train import main as train_main
    except ImportError:
        print(_FAIL_MSG, file=sys.stderr)
        return 3
    return int(train_main())


if __name__ == "__main__":
    raise SystemExit(main())
