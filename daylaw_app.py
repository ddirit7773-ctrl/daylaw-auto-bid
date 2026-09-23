from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

from dotenv import load_dotenv


def app_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


APP_ROOT = app_root()
os.chdir(APP_ROOT)
os.environ["DAYLAW_APP_ROOT"] = str(APP_ROOT)


def load_app_env() -> Path:
    """Load the portable sidecar .env from the same folder as the EXE.

    python-dotenv's implicit discovery can point at PyInstaller's internal bundle
    directory in a frozen app. Always using APP_ROOT makes the Windows portable
    layout deterministic.
    """
    env_path = APP_ROOT / ".env"
    if env_path.exists():
        load_dotenv(dotenv_path=env_path, override=True)
    return env_path


ENV_PATH = load_app_env()


BACKENDS = {
    "run_v2_scan": "run_v2_scan",
    "build_v2_delete_plan": "build_v2_delete_plan",
    "execute_v2_delete": "execute_v2_delete",
    "query_account_stats": "query_account_stats",
    "restore_deleted_keyword": "restore_deleted_keyword",
}


def run_backend(name: str, argv: list[str]) -> int:
    # Reload before every backend action so editing .env while the GUI is open
    # is picked up without reinstalling or moving files.
    load_app_env()
    module_name = BACKENDS.get(name)
    if not module_name:
        print(f"Unknown backend: {name}")
        return 2
    module = importlib.import_module(module_name)
    if not hasattr(module, "main"):
        print(f"Backend has no main(): {module_name}")
        return 2
    old_argv = sys.argv[:]
    try:
        sys.argv = [module_name + ".py", *argv]
        result = module.main()
        return int(result or 0)
    finally:
        sys.argv = old_argv


def _env_self_test() -> int:
    load_app_env()
    required = ("NAVER_API_KEY", "NAVER_SECRET_KEY", "NAVER_CUSTOMER_ID")
    missing = [name for name in required if not os.getenv(name, "").strip()]
    if missing:
        print("DAYLAW ENV SELF TEST FAILED: " + ", ".join(missing))
        return 3
    print("DAYLAW ENV SELF TEST OK")
    return 0


def main() -> int:
    if len(sys.argv) >= 2 and sys.argv[1] == "--self-test":
        import desktop_app_v4  # noqa: F401
        print("DAYLAW PACKAGED SELF TEST OK")
        return 0

    if len(sys.argv) >= 2 and sys.argv[1] == "--env-self-test":
        return _env_self_test()

    if len(sys.argv) >= 3 and sys.argv[1] == "--backend":
        return run_backend(sys.argv[2], sys.argv[3:])

    import desktop_app
    import desktop_app_v2
    import desktop_app_v3

    # Redirect all writable/readable app data to the portable application folder.
    desktop_app.ROOT_DIR = APP_ROOT
    desktop_app.BACKUP_DIR = APP_ROOT / "data" / "backups"
    desktop_app.CONFIG_DIR = APP_ROOT / "config"
    desktop_app_v2.ROOT_DIR = APP_ROOT
    desktop_app_v2.STATS_DIR = APP_ROOT / "data" / "stats"
    desktop_app_v3.ROOT_DIR = APP_ROOT
    desktop_app_v3.BACKUP_DIR = APP_ROOT / "data" / "backups"

    from desktop_app_v4 import DesktopAppV4

    app = DesktopAppV4()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
