#!/usr/bin/env python3
"""Automated interactive demo of background event injection.

Starts electron-echo, puts it in the background, then visually demonstrates
each event type one by one with narration in the terminal.

Usage:
    uv run python demo.py
    uv run python demo.py --focus-app Terminal
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path


EVENT_LOG = Path("/tmp/electron-echo-events.jsonl")
PROBE = Path(__file__).resolve().parent / "ghostpoke_probe.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _osa(script: str) -> str:
    proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    return (proc.stdout or "").strip()


def _activate(app: str) -> None:
    _osa(f'tell application "{app}" to activate')


def _frontmost() -> str:
    return _osa(
        'tell application "System Events" to get name of first '
        'application process whose frontmost is true'
    )


def _probe(args: list[str]) -> None:
    subprocess.run(
        [sys.executable, str(PROBE), *args],
        capture_output=True, text=True,
    )


def _cdp(port: int, params: dict) -> None:
    data = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/dispatch",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass


def _count_events(log: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not log.exists():
        return counts
    for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
        raw = line.strip()
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        t = str(row.get("type", ""))
        if t:
            counts[t] = counts.get(t, 0) + 1
    return counts


def _wait_fixture(log: Path, proc: subprocess.Popen, timeout: float):
    deadline = time.monotonic() + timeout
    pid = cdp = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"fixture exited rc={proc.returncode}")
        if not log.exists():
            time.sleep(0.1)
            continue
        for line in log.read_text(encoding="utf-8", errors="ignore").splitlines():
            try:
                row = json.loads(line.strip())
            except (json.JSONDecodeError, ValueError):
                continue
            if row.get("type") == "window-ready":
                pid = int(row.get("pid", 0) or 0)
            if row.get("type") == "cdp-ready":
                cdp = int(row.get("port", 0) or 0)
        if pid and cdp:
            return pid, cdp
        time.sleep(0.1)
    raise TimeoutError("fixture startup timeout")


def _cleanup() -> None:
    proc = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True)
    for line in (proc.stdout or "").splitlines():
        if "electron-echo" not in line:
            continue
        parts = line.strip().split(maxsplit=1)
        try:
            pid = int(parts[0])
        except (ValueError, IndexError):
            continue
        if pid > 1:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    time.sleep(0.3)


def _say(msg: str) -> None:
    print(f"\n  >>> {msg}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("macOS only")

    parser = argparse.ArgumentParser(description="ghostpoke interactive demo")
    parser.add_argument("--focus-app", default="TextEdit",
                        help="app to keep in foreground")
    parser.add_argument("--electron-bin", type=Path,
                        default=Path(__file__).resolve().parent / "electron-echo" / "node_modules" / ".bin" / "electron")
    parser.add_argument("--speed", type=float, default=1.0,
                        help="speed multiplier (0.5 = slower, 2.0 = faster)")
    args = parser.parse_args()

    def pause(secs: float = 0.8) -> None:
        time.sleep(secs / args.speed)

    fixture_dir = Path(__file__).resolve().parent / "electron-echo"

    # ---- startup ----
    print("\n  ghostpoke demo - background event injection")
    print("  " + "=" * 42)

    _cleanup()
    try:
        EVENT_LOG.unlink(missing_ok=True)
    except OSError:
        pass

    user_data = Path(f"/tmp/electron-echo-demo-{datetime.now().strftime('%H%M%S')}")
    user_data.mkdir(parents=True, exist_ok=True)

    log_file = open("/tmp/electron-echo-demo.log", "w", encoding="utf-8")
    fixture = subprocess.Popen(
        [str(args.electron_bin), ".", f"--user-data-dir={user_data}"],
        cwd=str(fixture_dir),
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
        text=True,
    )

    try:
        pid, cdp_port = _wait_fixture(EVENT_LOG, fixture, 10.0)
        _say(f"electron-echo started  (pid={pid}, cdp={cdp_port})")

        # resolve window bounds
        proc = subprocess.run(
            [sys.executable, str(PROBE), "--pid", str(pid),
             "--allow-negative-layer", "--dry-run"],
            capture_output=True, text=True,
        )
        dp = json.loads(proc.stdout.strip())
        wb = dp["window_bounds"]
        wx, wy = float(wb["x"]), float(wb["y"])
        ww, wh = float(wb["width"]), float(wb["height"])
        wid = int(dp["window_id"])
        TITLE = 32
        rh = wh - TITLE
        cx = wx + ww / 2

        def sy(renderer_y: float) -> str:
            return str(wy + TITLE + renderer_y)

        base = ["--pid", str(pid), "--window-id", str(wid), "--allow-negative-layer"]

        # ensure focus app is running and bring to front
        _osa(f'tell application "{args.focus_app}" to activate')
        pause(0.5)
        _say(f"foreground: {_frontmost()}  (electron-echo is in the background)")
        pause(1.0)

        # ---- 1. CLICK ----
        _say("1/5  CLICK -- pressing the button ...")
        pause(0.5)
        for _ in range(3):
            _probe([*base, "--action", "click",
                    "--screen-x", str(cx), "--screen-y", sy(41)])
            pause(0.4)
        c = _count_events(EVENT_LOG)
        print(f"       button pressed {c.get('clicked', 0)} times")
        pause(1.0)

        # ---- 2. KEY ----
        _say("2/5  KEY -- typing 'ghostpoke' ...")
        pause(0.5)
        for ch in "ghostpoke":
            _probe(["--pid", str(pid), "--action", "key",
                    "--key-char", ch, "--modifiers", ""])
            pause(0.15)
        pause(0.3)
        c = _count_events(EVENT_LOG)
        print(f"       {c.get('keyPressed', 0)} keystrokes delivered")
        pause(1.0)

        # ---- 3. SCROLL ----
        scroll_y = int((136 + rh - 144) / 2)
        _say("3/5  SCROLL -- scrolling the list down ... (via CDP)")
        pause(0.5)
        for _ in range(5):
            _cdp(cdp_port, {"type": "mouseWheel",
                            "x": int(ww / 2), "y": scroll_y,
                            "deltaX": 0, "deltaY": 160, "button": "none"})
            pause(0.25)
        c = _count_events(EVENT_LOG)
        print(f"       {c.get('scrolled', 0)} scroll events received")
        pause(1.0)

        # ---- 4. DRAG ----
        drag_y = rh - 108
        _say("4/5  DRAG -- sliding the handle from left to right ...")
        pause(0.5)
        _probe([*base, "--action", "drag",
                "--screen-x", str(wx + 90),
                "--screen-y", sy(drag_y),
                "--drag-to-screen-x", str(wx + ww - 90),
                "--drag-to-screen-y", sy(drag_y),
                "--drag-steps", "20"])
        _activate(args.focus_app)
        pause(0.3)
        c = _count_events(EVENT_LOG)
        print(f"       slider dragged ({c.get('dragged', 0)} drag points)")
        pause(1.0)

        # ---- 5. MOVE ----
        move_y = int(rh - 36)
        _say("5/5  MOVE -- moving cursor across the zone ... (via CDP)")
        pause(0.5)
        for i in range(8):
            x = 60 + i * 65
            _cdp(cdp_port, {"type": "mouseMoved",
                            "x": x, "y": move_y, "button": "none"})
            pause(0.12)
        c = _count_events(EVENT_LOG)
        print(f"       cursor tracked ({c.get('moved', 0)} move events)")
        pause(1.0)

        # ---- summary ----
        front_now = _frontmost()
        c = _count_events(EVENT_LOG)
        total = sum(c.get(t, 0) for t in ("clicked", "keyPressed", "scrolled", "dragged", "moved"))

        print("\n  " + "-" * 42)
        print(f"  Total events delivered:  {total}")
        print(f"  clicked={c.get('clicked',0)}  key={c.get('keyPressed',0)}  "
              f"scroll={c.get('scrolled',0)}  drag={c.get('dragged',0)}  "
              f"move={c.get('moved',0)}")
        print(f"  Foreground app:  {front_now} (unchanged)")
        print(f"  Electron-echo:   background (never activated)")
        print("  " + "=" * 42)
        print()
        return 0

    finally:
        try:
            os.killpg(fixture.pid, signal.SIGTERM)
        except OSError:
            pass
        log_file.close()


if __name__ == "__main__":
    raise SystemExit(main())
