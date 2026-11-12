"""The daily scheduled scout, via launchd on macOS.

A per-user LaunchAgent with a calendar trigger. launchd, unlike cron, runs a
job whose time passed while the Mac was asleep as soon as it wakes (several
missed times collapse into one run). It does not run while the Mac is
powered off or the user is logged out.
"""

import os
import plistlib
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

LABEL = "com.getrolescout.scout"
DEFAULT_LOG_DIR = Path("~/Library/Logs/rolescout")
DEFAULT_BACKUP_DIR = Path("~/.rolescout/backups")
LOG_NAME = "scout.log"
LAUNCHD_LOG_NAME = "launchd.log"   # only what is printed before rolescout's own log opens


class ScheduleError(Exception):
    pass


def is_macos() -> bool:
    return sys.platform == "darwin"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def parse_time(text: str) -> tuple[int, int]:
    try:
        hour, minute = (int(part) for part in text.strip().split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError:
        raise ScheduleError(f"time must be HH:MM in 24-hour local time, got {text!r}") from None
    return hour, minute


def cron_equivalent(hour: int, minute: int, db: Path, backup_dir: Path, keep: int, log: Path) -> str:
    return (f"{minute} {hour} * * * {sys.executable} -m rolescout daily --db {db} "
            f"--backup-dir {backup_dir} --keep {keep} --log {log}")


def build_plist(hour: int, minute: int, db: Path, backup_dir: Path, keep: int, log_dir: Path) -> dict:
    return {
        "Label": LABEL,
        # The interpreter rolescout is installed in, so the job needs no PATH.
        "ProgramArguments": [
            sys.executable, "-m", "rolescout", "daily",
            "--db", str(db), "--backup-dir", str(backup_dir), "--keep", str(keep),
            "--log", str(log_dir / LOG_NAME)],
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "RunAtLoad": False,
        "ProcessType": "Background",
        "StandardOutPath": str(log_dir / LAUNCHD_LOG_NAME),
        "StandardErrorPath": str(log_dir / LAUNCHD_LOG_NAME),
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domains() -> list[str]:
    uid = os.getuid()
    # gui/ is the logged-in desktop session; user/ is the fallback when there
    # is none (for example when installing over SSH on a headless Mac).
    return [f"gui/{uid}", f"user/{uid}"]


def loaded_domain() -> str | None:
    for domain in _domains():
        if _launchctl("print", f"{domain}/{LABEL}").returncode == 0:
            return domain
    return None


def install(hour: int, minute: int, *, db: Path, backup_dir: Path, keep: int, log_dir: Path) -> dict:
    """Write the LaunchAgent and load it. Replaces any existing schedule."""
    if not is_macos():
        raise ScheduleError(
            "scheduling is built for macOS (launchd) only. On this system add a cron entry "
            "yourself, for example:\n  "
            + cron_equivalent(hour, minute, db, backup_dir, keep, log_dir / LOG_NAME)
            + "\nNote: cron skips runs missed while the machine is asleep or off.")
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)
    for domain in _domains():
        _launchctl("bootout", f"{domain}/{LABEL}")  # fine if it was not loaded
    with open(path, "wb") as f:
        plistlib.dump(build_plist(hour, minute, db, backup_dir, keep, log_dir), f)

    errors = []
    for domain in _domains():
        result = _launchctl("bootstrap", domain, str(path))
        if result.returncode == 0:
            return {"plist": path, "domain": domain, "log": log_dir / LOG_NAME,
                    "backup_dir": backup_dir}
        errors.append(f"{domain}: {(result.stderr or result.stdout).strip()}")
    raise ScheduleError(
        f"wrote {path} but launchd would not load it:\n  " + "\n  ".join(errors))


def remove() -> bool:
    """Unload and delete the LaunchAgent. Returns False if there was none."""
    if not is_macos():
        raise ScheduleError("scheduling is built for macOS (launchd) only; "
                            "nothing was installed by rolescout on this system.")
    path = plist_path()
    was_loaded = False
    for domain in _domains():
        was_loaded = _launchctl("bootout", f"{domain}/{LABEL}").returncode == 0 or was_loaded
    existed = path.exists()
    path.unlink(missing_ok=True)
    return existed or was_loaded


def next_run(hour: int, minute: int, now: datetime | None = None) -> datetime:
    """The next local time the calendar trigger fires."""
    now = now or datetime.now()
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + timedelta(days=1)


def read_schedule(now: datetime | None = None) -> dict:
    """The installed schedule, read from the plist and launchd. No network."""
    if not is_macos():
        return {"supported": False, "installed": False, "loaded": False}
    path = plist_path()
    info: dict = {"supported": True, "installed": path.exists(), "loaded": False, "plist": str(path)}
    if not info["installed"]:
        return info
    try:
        with open(path, "rb") as f:
            plist = plistlib.load(f)
        when = plist["StartCalendarInterval"]
        hour, minute = int(when["Hour"]), int(when["Minute"])
        args = plist["ProgramArguments"]
    except Exception as exc:
        info["error"] = f"could not read {path}: {exc}"
        return info

    def arg(flag: str) -> str | None:
        return args[args.index(flag) + 1] if flag in args and args.index(flag) + 1 < len(args) else None

    info.update({
        "time": f"{hour:02d}:{minute:02d}", "next_run": next_run(hour, minute, now).strftime("%Y-%m-%d %H:%M"),
        "database": arg("--db"), "backup_dir": arg("--backup-dir"), "log": arg("--log"),
        "domain": loaded_domain()})
    info["loaded"] = info["domain"] is not None
    return info
