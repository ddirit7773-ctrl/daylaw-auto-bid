from __future__ import annotations

import daylaw_app as base


# Keep every existing backend route unchanged except the immediate-review gate.
# The wrapper runs the existing full live revalidation first, then approves only
# the independently safe READY subset while leaving blocked rows pending.
base.BACKENDS["approve_immediate_20"] = "approve_immediate_partial"


if __name__ == "__main__":
    raise SystemExit(base.main())
