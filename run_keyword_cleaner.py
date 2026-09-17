from __future__ import annotations

import sys

from src.keyword_cleaner.main import main


if __name__ == "__main__":
    # The legacy scanner is still useful for read-only analysis, but its old
    # live-delete path predates the final lifecycle policy (status eligibility,
    # data completeness, two-pass confirmation, archive/restore). Keep deletion
    # locked until the v2 executor is connected.
    if "--delete" in sys.argv:
        print("[SAFE STOP] Legacy live deletion is disabled while Cleaner V2 is being built.")
        print("Run without --delete for read-only analysis. No keyword was changed.")
        raise SystemExit(2)
    raise SystemExit(main())
