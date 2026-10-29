"""Update affordance backend for the SYSTEM panel (M7 P5).

The control plane never writes partitions: ``lemonseed-update`` does, and this
module's whole job is to run it as a *job* — 202 plus a pollable status, the
same shape the OWUI reset uses — and to publish running vs latest available.

The failed-update detector rests on one fact the appliance only just started
writing down: when the updater arms GRUB ``boot_once`` it also records
``{intended_version, target_slot}`` (update-attempt.json) — the only moment the
target is ever known. After the reboot, booting any slot other than the target
while running anything other than the intended version is the failed-or-stuck
signature the panel shows as a banner with *Try again*; booting the target (or
running the intended version) clears the record. Before this existed, a failed
update was invisible: the appliance knew only the version it was running.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

import chat_ready
import modules
import owui_policy
import settings

UPDATER = "lemonseed-update"  # packaged in nixos/packages/lemonseed-update.nix
ATTEMPT_FILE = Path("/var/lib/lemonseed/update-attempt.json")
LATEST_FILE = Path("/var/lib/lemonseed/latest-update.json")
SLOT_VERSIONS_FILE = Path("/var/lib/lemonseed/slot-versions.json")
CMDLINE = Path("/proc/cmdline")

# The interlock refuses while one of these runs; each names itself.
_JOB_BUSY = "a system update is running"

_job_lock = threading.Lock()
# kind: "" | "check" | "install"; state: idle | running | downloading |
# writing | rebooting | done | failed. Read by the panel's poll loop.
# progress: the updater's own stdout percentage, or None when it has not
# printed one (the panel's bar treats None as "started, no number yet").
_job: dict[str, Any] = {"kind": "", "state": "idle", "detail": "", "progress": None}
# The updater formats percentages right-aligned ("[ 42%]"), so allow space
# between the bracket and the digits.
_PROGRESS = re.compile(r"\[\s*(\d{1,3})%")
# The running job's thread, so callers (and tests) can wait for it to end.
_job_thread: Optional[threading.Thread] = None


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def booted_slot() -> Optional[str]:
    """The slot the running kernel was booted from, or None when unknown.

    Mirrors the updater's own ``cmdline_slot``: the command line is the truth
    for "which slot am I", while ``boot_default`` is only what GRUB fell back
    to.
    """
    try:
        text = CMDLINE.read_text(encoding="utf-8")
    except OSError:
        return None
    for arg in text.split():
        if arg.startswith("lemonseed.slot="):
            slot = arg.split("=", 1)[1]
            if slot in ("a", "b"):
                return slot
    return None


def failed_update(running: str) -> Optional[dict[str, Any]]:
    """The failed-update banner's content, else ``None``.

    An arm-time record exists while the booted slot is not the target and the
    running version is not the intended one — the failed-or-stuck signature.
    Booting the target (or running the intended version) clears the record, so
    the banner is shown exactly once per failed attempt.
    """
    attempt = _read_json(ATTEMPT_FILE)
    if not attempt:
        return None
    target = str(attempt.get("target_slot") or "")
    intended = str(attempt.get("intended_version") or "")
    booted = booted_slot()
    if booted == target or (bool(intended) and running == intended):
        clear_attempt()
        return None
    if booted is None:
        # The slot is unknowable (no lemonseed.slot on the command line, e.g.
        # a lab boot): saying "failed" would be a guess, so say nothing and
        # leave the record for the next boot.
        return None
    return {
        "intended": intended,
        "running": running,
        "attempted_at": str(attempt.get("attempted_at") or ""),
    }


def clear_attempt() -> None:
    ATTEMPT_FILE.unlink(missing_ok=True)


def rollback_eligibility(running: str) -> tuple[bool, str]:
    """(eligible, reason) for the panel's rollback affordance (M8 P6, D30).

    The slot-version registry is the authority: the other slot must hold a
    recorded version that differs from the running one and sits at or above
    the running release's declared rollback floor. The updater's own verb
    re-checks everything root-side — this is the panel's cheap gate so an
    ineligible button is hidden, not pressed.
    """
    registry = _read_json(SLOT_VERSIONS_FILE) or {}
    slots = registry.get("slots") if isinstance(registry.get("slots"), dict) else {}
    booted = booted_slot()
    other = "b" if booted == "a" else ("a" if booted == "b" else "")
    if not other:
        return False, "the active slot could not be determined"
    entry = slots.get(other)
    prior = str((entry or {}).get("version") or "") if isinstance(entry, dict) else ""
    if not prior:
        return False, "the other slot has no version recorded — nothing to roll back to"
    if prior == running:
        return False, "the prior version is the one already running"
    floor_entry = slots.get(booted or "") or {}
    floor = str((floor_entry or {}).get("min_rollback_version") or "")
    if floor and prior < floor:
        return False, (
            f"the prior version {prior} is below the rollback floor {floor} "
            f"declared by {running}"
        )
    return True, ""


def snapshot(running: str) -> dict[str, Any]:
    """Everything the SYSTEM panel's Version section renders, in one read."""
    with _job_lock:
        job = dict(_job)
    latest_record = _read_json(LATEST_FILE) or {}
    latest = str(latest_record.get("latest") or "")
    # Versions are date stamps, so a stale record (written before the last
    # update landed) compares below the running version and must not offer a
    # downgrade — only a strictly newer channel version is "available". A
    # rollback marker suppresses offers at or below the version the operator
    # fled to (M8 P6, D30), until a strictly newer one is published.
    registry = _read_json(SLOT_VERSIONS_FILE) or {}
    slots = registry.get("slots") if isinstance(registry.get("slots"), dict) else {}
    booted = booted_slot()
    other = "b" if booted == "a" else ("a" if booted == "b" else "")
    prior = str((slots.get(other) or {}).get("version") or "")
    rolled_back = str(registry.get("rolled_back_version") or "")
    eligible, reason = rollback_eligibility(running)
    available = (
        bool(latest)
        and latest != running
        and latest > running
        and not (rolled_back and latest <= rolled_back)
    )
    return {
        "running": running,
        "latest": latest,
        "available": available,
        "prior": prior,
        "rolled_back": rolled_back,
        "rollback_eligible": eligible,
        "rollback_reason": reason,
        # The other slot is not always older: after a rollback it holds the
        # newer release, and the affordance is a roll *forward*. The label
        # follows the direction; the operation is the same boot-pointer flip.
        "rollback_kind": ("forward" if prior > running else "back") if prior else "back",
        "checked_at": str(latest_record.get("checked_at") or ""),
        "job": job,
        "failed": failed_update(running),
    }


def in_flight() -> Optional[str]:
    """Power-interlock sentence while an install job runs, else ``None``.

    A reboot in the middle of an install is exactly what the installer is
    doing anyway, but an *admin* pressing reboot mid-download competes with
    the updater for the same disk, so the power controls ask this too. A check
    is a read and never blocks anything.
    """
    with _job_lock:
        kind = str(_job.get("kind") or "")
        state = str(_job.get("state") or "idle")
    if kind in ("install", "rollback") and state in ("running", "downloading", "writing", "rebooting"):
        return _JOB_BUSY
    return None


def _interlock_reason() -> Optional[str]:
    return (
        chat_ready.in_flight_work()
        or modules.in_flight_work()
        or ("the Open WebUI settings are being rebuilt" if owui_policy.reconcile_in_flight() else None)
    )


def start_check() -> Optional[str]:
    """Record the latest available version. ``None`` when the job started."""
    command = [UPDATER, "check", "--record"]
    channel = settings.get_setting("release_channel", "stable")
    if channel:
        command.extend(["--channel", channel])
    return _start("check", [command])


def start_install(*, retry: bool = False) -> Optional[str]:
    """Download, write the inactive slot, and reboot (D4). ``None`` when started.

    ``retry`` is the failed-update banner's *Try again*: cancel-then-apply,
    never bare ``apply`` — a failed attempt leaves ``boot_attempted`` set, and
    ``refuse_dirty_state`` would (rightly) refuse the re-apply without the
    cancel in front of it.
    """
    commands: list[list[str]] = []
    if retry:
        commands.append([UPDATER, "cancel"])
    command = [UPDATER, "apply", "--reboot"]
    channel = settings.get_setting("release_channel", "stable")
    if channel:
        command.extend(["--channel", channel])
    commands.append(command)
    return _start("install", commands)


def start_rollback(running: str) -> Optional[str]:
    """Boot the prior slot and reboot (M8 P6, D30). ``None`` when started.

    The verb is a boot-pointer flip through the same tested primitives apply
    uses, so the job shape is identical — including the reboot tail the panel
    watches. The eligibility gate mirrors the updater's own (the registry is
    the authority on both sides); the updater refuses again root-side, and a
    refusal lands in the job status, not in a silently broken slot.
    """
    eligible, reason = rollback_eligibility(running)
    if not eligible:
        return reason
    return _start("rollback", [[UPDATER, "rollback", "--reboot"]])


def _start(kind: str, commands: list[list[str]]) -> Optional[str]:
    global _job_thread
    with _job_lock:
        state = str(_job.get("state") or "idle")
        # Only a job that is actually *running* blocks a new one. A finished
        # job stays visible as done/failed for the panel, but refusing a new
        # start because of a corpse locked the check button until a reboot
        # (bare-metal review) — the lock is the state, not the last result.
        if state in ("running", "downloading", "writing", "rebooting"):
            previous = str(_job.get("kind") or "system update")
            return f"a {previous} job is already running"
        reason = _interlock_reason()
        if reason is None:
            _job.update(kind=kind, state="running", detail="", progress=None)
            thread = threading.Thread(target=_run_job, args=(kind, commands), daemon=True)
            thread.start()
            _job_thread = thread
            return None
    return reason


def _run_job(kind: str, commands: list[list[str]]) -> None:
    """The job thread: run the updater's steps in order, naming the phase.

    The updater's log lines carry the milestones, and the phase is read off
    them rather than maintained as a second state machine.
    """
    for command in commands:
        _set_state("running", "")
        try:
            proc = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                errors="replace",
            )
        except OSError as exc:
            _finish(kind, False, str(exc))
            return
        assert proc.stdout is not None
        last_line = ""
        for raw in proc.stdout:
            line = raw.strip()
            if not line:
                continue
            last_line = line
            _phase_from_line(line)
        code = proc.wait()
        if code != 0:
            detail = f"lemonseed-update {command[1]} failed (exit {code})"
            if last_line:
                detail = f"{detail}: {last_line}"
            _finish(kind, False, detail)
            return
        if kind == "install" and command[1] == "cancel":
            continue
    _finish(kind, True, "")


def _set_state(state: str, detail: str) -> None:
    with _job_lock:
        _job["state"] = state
        _job["detail"] = detail


def _phase_from_line(line: str) -> None:
    """Map one updater log line onto the job: a phase, a percent, or both.

    The download and write steps print ``[ 42%] …``; the number rides straight
    through to the panel's progress bar. Phases come from the milestones, and
    the reboot milestone also writes the going-away marker the shell reads.
    """
    lowered = line.lower()
    state = ""
    if "fetch" in lowered or "downloading" in lowered:
        state = "downloading"
    elif "writing squashfs" in lowered or "esp kernels" in lowered or "grub.cfg" in lowered:
        state = "writing"
    elif "rebooting" in lowered:
        state = "rebooting"
    match = _PROGRESS.search(line)
    if not state and not match:
        return
    rebooting = state == "rebooting"
    with _job_lock:
        if state:
            _job["state"] = state
            _job["detail"] = line
        if match:
            _job["progress"] = min(int(match.group(1)), 100)
    if rebooting:
        # The update's own reboot rides the same going-away path the power
        # buttons use, so the shell says "rebooting" instead of a dead poll.
        chat_ready.mark_reboot_pending("the system update is restarting the appliance")


def _finish(kind: str, ok: bool, detail: str) -> None:
    with _job_lock:
        state = "done" if ok else "failed"
        # An install that reached its own reboot stays "rebooting": the box is
        # on its way down and nothing else will run to move the status on.
        if ok and kind == "install" and str(_job.get("state") or "") == "rebooting":
            state = "rebooting"
        _job["state"] = state
        _job["detail"] = detail[:300]
    if not ok and kind == "install":
        # A failed install never reached the reboot, so the going-away marker
        # (written when "rebooting now" was seen) must not outlive it.
        chat_ready.clear_reboot_pending()
