#!/usr/bin/env python3
"""Probe Codex-style background events on macOS.

Supported actions (--action):
  click   Mouse click (left/right/middle, multi-click)
  move    Mouse move to a target point
  drag    Mouse drag from start to end point
  scroll  Scroll wheel (vertical/horizontal)
  key     Keyboard key press with optional modifiers

Core path for mouse events:
1) Build NSEvent with target windowNumber.
2) Convert NSEvent -> CGEvent.
3) Set CGEvent integer fields (button, subtype, window pointers).
4) Set screen-space location.
5) Optionally set private CGEventSetWindowLocation via dlsym.
6) If target app is backgrounded, optionally set kCGEventFlagMaskCommand.
7) Post event directly to target pid via CGEventPostToPid.

Keyboard and scroll events use CGEventCreate*Event + CGEventPostToPid directly.
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import json
import os
import sys
import time
from dataclasses import dataclass


def _ensure_macos() -> None:
    if sys.platform != "darwin":
        raise SystemExit("ghostpoke_probe.py only works on macOS.")


@dataclass
class WindowCandidate:
    window_id: int
    layer: int
    owner_name: str
    title: str
    x: float
    y: float
    width: float
    height: float
    alpha: float

    @property
    def area(self) -> float:
        return self.width * self.height


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


# macOS virtual key codes (Carbon HIToolbox/Events.h)
KEYCODE_MAP: dict[str, int] = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04,
    "g": 0x05, "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09,
    "b": 0x0B, "q": 0x0C, "w": 0x0D, "e": 0x0E, "r": 0x0F,
    "y": 0x10, "t": 0x11, "1": 0x12, "2": 0x13, "3": 0x14,
    "4": 0x15, "6": 0x16, "5": 0x17, "9": 0x19, "7": 0x1A,
    "8": 0x1C, "0": 0x1D, "o": 0x1F, "u": 0x20, "i": 0x22,
    "p": 0x23, "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D,
    "m": 0x2E,
    "return": 0x24, "tab": 0x30, "space": 0x31, "delete": 0x33,
    "escape": 0x35, "forwarddelete": 0x75,
    "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76,
    "f5": 0x60, "f6": 0x61, "f7": 0x62, "f8": 0x64,
    "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
}


def _resolve_modifier_flags(quartz, modifiers: str) -> int:
    """Parse comma-separated modifier names into CGEvent flags bitmask."""
    flags = 0
    for mod in modifiers.lower().split(","):
        mod = mod.strip()
        if not mod:
            continue
        if mod in ("command", "cmd"):
            flags |= int(quartz.kCGEventFlagMaskCommand)
        elif mod == "shift":
            flags |= int(quartz.kCGEventFlagMaskShift)
        elif mod in ("option", "alt"):
            flags |= int(quartz.kCGEventFlagMaskAlternate)
        elif mod in ("control", "ctrl"):
            flags |= int(quartz.kCGEventFlagMaskControl)
        else:
            raise ValueError(f"unknown modifier: {mod}")
    return flags


def _load_private_window_location_setter():
    """Best-effort load of private CGEventSetWindowLocation."""
    paths = []
    for name in ("ApplicationServices", "CoreGraphics"):
        lib = ctypes.util.find_library(name)
        if lib:
            paths.append(lib)
    paths.extend([
        "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices",
        "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics",
    ])

    seen: set[str] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        try:
            dylib = ctypes.CDLL(path)
        except OSError:
            continue
        try:
            func = dylib.CGEventSetWindowLocation
        except AttributeError:
            continue
        func.argtypes = [ctypes.c_void_p, CGPoint]
        func.restype = None
        return func
    return None


def _resolve_pid(appkit, query: str | None, explicit_pid: int | None) -> int:
    if explicit_pid is not None and explicit_pid > 0:
        return int(explicit_pid)

    normalized = (query or "").strip().lower()
    if not normalized:
        raise RuntimeError("provide --pid or --app")

    apps = appkit.NSWorkspace.sharedWorkspace().runningApplications()
    for app in apps:
        try:
            pid = int(app.processIdentifier())
            name = str(app.localizedName() or "")
            bundle_id = str(app.bundleIdentifier() or "")
        except Exception:
            continue
        if not name and not bundle_id:
            continue
        if (
            normalized == name.lower()
            or normalized in name.lower()
            or normalized == bundle_id.lower()
        ):
            return pid

    raise RuntimeError(f"cannot resolve app: {query}")


def _window_candidates_for_pid(quartz, pid: int) -> list[WindowCandidate]:
    info_list = quartz.CGWindowListCopyWindowInfo(
        quartz.kCGWindowListOptionOnScreenOnly,
        quartz.kCGNullWindowID,
    )
    candidates: list[WindowCandidate] = []
    for info in info_list or []:
        owner_pid = int(info.get(quartz.kCGWindowOwnerPID, 0))
        if owner_pid != int(pid):
            continue

        bounds = info.get(quartz.kCGWindowBounds, {}) or {}
        width = float(bounds.get("Width", 0.0))
        height = float(bounds.get("Height", 0.0))
        if width <= 0 or height <= 0:
            continue

        candidates.append(
            WindowCandidate(
                window_id=int(info.get(quartz.kCGWindowNumber, 0)),
                layer=int(info.get(quartz.kCGWindowLayer, 0)),
                owner_name=str(info.get(quartz.kCGWindowOwnerName, "") or ""),
                title=str(info.get(quartz.kCGWindowName, "") or ""),
                x=float(bounds.get("X", 0.0)),
                y=float(bounds.get("Y", 0.0)),
                width=width,
                height=height,
                alpha=float(info.get(quartz.kCGWindowAlpha, 1.0)),
            )
        )
    return candidates


def _pick_window(
    candidates: list[WindowCandidate],
    explicit_window_id: int | None,
    *,
    exclude_negative_layer: bool,
    prefer_layer0: bool,
    min_alpha: float,
) -> WindowCandidate:
    if explicit_window_id is not None:
        for item in candidates:
            if int(item.window_id) == int(explicit_window_id):
                return item
        raise RuntimeError(f"window_id={explicit_window_id} not found for target pid")

    pool = list(candidates)
    if exclude_negative_layer:
        non_negative = [item for item in pool if int(item.layer) >= 0]
        if not non_negative:
            raise RuntimeError(
                "no non-negative-layer window found; rerun with --allow-negative-layer "
                "or pass --window-id explicitly"
            )
        pool = non_negative
    opaque = [item for item in pool if float(item.alpha) >= float(min_alpha)]
    if opaque:
        pool = opaque
    if prefer_layer0:
        layer0 = [item for item in pool if int(item.layer) == 0]
        if layer0:
            pool = layer0
    if not pool:
        raise RuntimeError("no visible windows for target pid")
    pool.sort(key=lambda item: item.area, reverse=True)
    return pool[0]


def _app_is_active(appkit, pid: int) -> bool:
    app = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
    if app is None:
        return False
    try:
        return bool(app.isActive())
    except Exception:
        return False


def _frontmost_app_name(appkit) -> str:
    try:
        app = appkit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return ""
        return str(app.localizedName() or "")
    except Exception:
        return ""


def _infer_electron_app(appkit, pid: int) -> bool:
    app = appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_(int(pid))
    if app is None:
        return False
    bundle_url = app.bundleURL()
    if bundle_url is None:
        return False
    try:
        bundle_path = str(bundle_url.path() or "")
    except Exception:
        return False
    if not bundle_path:
        return False
    framework_path = os.path.join(
        bundle_path,
        "Contents",
        "Frameworks",
        "Electron Framework.framework",
    )
    return os.path.exists(framework_path)


def _make_nsevent(appkit, event_type, local_x: float, local_y: float, window_id: int, click_count: int, event_number: int):
    return appkit.NSEvent.mouseEventWithType_location_modifierFlags_timestamp_windowNumber_context_eventNumber_clickCount_pressure_(
        event_type,
        appkit.NSMakePoint(local_x, local_y),
        0,  # modifier flags set later on CGEvent
        appkit.NSProcessInfo.processInfo().systemUptime(),
        int(window_id),
        None,
        int(event_number),
        int(click_count),
        1.0,
    )


def _button_spec(quartz, appkit, mouse_button: str):
    value = mouse_button.strip().lower()
    if value == "right":
        return (
            quartz.kCGMouseButtonRight,
            appkit.NSEventTypeRightMouseDown,
            appkit.NSEventTypeRightMouseUp,
            1,
        )
    if value == "middle":
        return (
            quartz.kCGMouseButtonCenter,
            appkit.NSEventTypeOtherMouseDown,
            appkit.NSEventTypeOtherMouseUp,
            2,
        )
    return (
        quartz.kCGMouseButtonLeft,
        appkit.NSEventTypeLeftMouseDown,
        appkit.NSEventTypeLeftMouseUp,
        0,
    )


def _apply_fields(quartz, event, *, button_index: int, window_id: int, subtype_value: int, include_subtype: bool, include_window_fields: bool):
    quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventButtonNumber, int(button_index))
    if include_subtype:
        quartz.CGEventSetIntegerValueField(event, quartz.kCGMouseEventSubtype, int(subtype_value))
    if include_window_fields:
        quartz.CGEventSetIntegerValueField(
            event,
            quartz.kCGMouseEventWindowUnderMousePointer,
            int(window_id),
        )
        quartz.CGEventSetIntegerValueField(
            event,
            quartz.kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent,
            int(window_id),
        )


def _post_click(
    *,
    quartz,
    appkit,
    objc_mod,
    setter_window_location,
    pid: int,
    window_id: int,
    screen_x: float,
    screen_y: float,
    local_x: float,
    local_y: float,
    click_count: int,
    mouse_button: str,
    include_subtype: bool,
    subtype_value: int,
    include_window_fields: bool,
    include_mask_command_when_bg: bool,
    include_window_local: bool,
) -> None:
    _cg_button, down_type, up_type, button_index = _button_spec(quartz, appkit, mouse_button)
    event_number_seed = int(time.time_ns() & 0x7FFFFFFF)

    for i in range(max(1, int(click_count))):
        click_state = i + 1
        down_event = _make_nsevent(
            appkit,
            down_type,
            local_x,
            local_y,
            window_id,
            click_state,
            event_number_seed + i * 2,
        ).CGEvent()
        up_event = _make_nsevent(
            appkit,
            up_type,
            local_x,
            local_y,
            window_id,
            click_state,
            event_number_seed + i * 2 + 1,
        ).CGEvent()

        for cg_event in (down_event, up_event):
            _apply_fields(
                quartz,
                cg_event,
                button_index=button_index,
                window_id=window_id,
                subtype_value=subtype_value,
                include_subtype=include_subtype,
                include_window_fields=include_window_fields,
            )
            quartz.CGEventSetLocation(cg_event, quartz.CGPointMake(float(screen_x), float(screen_y)))

            if include_mask_command_when_bg and (not _app_is_active(appkit, pid)):
                quartz.CGEventSetFlags(cg_event, quartz.kCGEventFlagMaskCommand)

            if include_window_local and setter_window_location is not None:
                setter_window_location(
                    ctypes.c_void_p(int(objc_mod.pyobjc_id(cg_event))),
                    CGPoint(float(local_x), float(local_y)),
                )

            quartz.CGEventPostToPid(int(pid), cg_event)
        time.sleep(0.03)


def _post_move(
    *,
    quartz,
    appkit,
    objc_mod,
    setter_window_location,
    pid: int,
    window_id: int,
    screen_x: float,
    screen_y: float,
    local_x: float,
    local_y: float,
    include_window_fields: bool,
    include_window_local: bool,
) -> None:
    event_number = int(time.time_ns() & 0x7FFFFFFF)
    ns_event = _make_nsevent(
        appkit, appkit.NSEventTypeMouseMoved,
        local_x, local_y, window_id, 0, event_number,
    )
    cg_event = ns_event.CGEvent()
    quartz.CGEventSetLocation(cg_event, quartz.CGPointMake(float(screen_x), float(screen_y)))
    if include_window_fields:
        quartz.CGEventSetIntegerValueField(
            cg_event, quartz.kCGMouseEventWindowUnderMousePointer, int(window_id),
        )
        quartz.CGEventSetIntegerValueField(
            cg_event, quartz.kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent, int(window_id),
        )
    if include_window_local and setter_window_location is not None:
        setter_window_location(
            ctypes.c_void_p(int(objc_mod.pyobjc_id(cg_event))),
            CGPoint(float(local_x), float(local_y)),
        )
    quartz.CGEventPostToPid(int(pid), cg_event)


def _post_drag(
    *,
    quartz,
    appkit,
    objc_mod,
    setter_window_location,
    pid: int,
    window_id: int,
    screen_x_start: float,
    screen_y_start: float,
    local_x_start: float,
    local_y_start: float,
    screen_x_end: float,
    screen_y_end: float,
    local_x_end: float,
    local_y_end: float,
    mouse_button: str,
    steps: int,
    include_subtype: bool,
    subtype_value: int,
    include_window_fields: bool,
    include_mask_command_when_bg: bool,
    include_window_local: bool,
) -> None:
    _cg_button, down_type, up_type, button_index = _button_spec(quartz, appkit, mouse_button)
    drag_type_map = {
        "left": appkit.NSEventTypeLeftMouseDragged,
        "right": appkit.NSEventTypeRightMouseDragged,
        "middle": appkit.NSEventTypeOtherMouseDragged,
    }
    drag_type = drag_type_map.get(mouse_button.strip().lower(), appkit.NSEventTypeLeftMouseDragged)
    event_number_seed = int(time.time_ns() & 0x7FFFFFFF)

    def _post_one(cg_ev, sx, sy, lx, ly):
        _apply_fields(
            quartz, cg_ev, button_index=button_index, window_id=window_id,
            subtype_value=subtype_value, include_subtype=include_subtype,
            include_window_fields=include_window_fields,
        )
        quartz.CGEventSetLocation(cg_ev, quartz.CGPointMake(float(sx), float(sy)))
        if include_mask_command_when_bg and not _app_is_active(appkit, pid):
            quartz.CGEventSetFlags(cg_ev, quartz.kCGEventFlagMaskCommand)
        if include_window_local and setter_window_location is not None:
            setter_window_location(
                ctypes.c_void_p(int(objc_mod.pyobjc_id(cg_ev))),
                CGPoint(float(lx), float(ly)),
            )
        quartz.CGEventPostToPid(int(pid), cg_ev)

    # mouse down at start
    down_ev = _make_nsevent(
        appkit, down_type, local_x_start, local_y_start,
        window_id, 1, event_number_seed,
    ).CGEvent()
    _post_one(down_ev, screen_x_start, screen_y_start, local_x_start, local_y_start)
    time.sleep(0.02)

    # intermediate drag steps
    actual_steps = max(1, int(steps))
    for i in range(1, actual_steps + 1):
        t = i / actual_steps
        sx = screen_x_start + (screen_x_end - screen_x_start) * t
        sy = screen_y_start + (screen_y_end - screen_y_start) * t
        lx = local_x_start + (local_x_end - local_x_start) * t
        ly = local_y_start + (local_y_end - local_y_start) * t
        drag_ev = _make_nsevent(
            appkit, drag_type, lx, ly, window_id, 0, event_number_seed + i,
        ).CGEvent()
        _post_one(drag_ev, sx, sy, lx, ly)
        time.sleep(0.01)

    # mouse up at end
    up_ev = _make_nsevent(
        appkit, up_type, local_x_end, local_y_end,
        window_id, 1, event_number_seed + actual_steps + 1,
    ).CGEvent()
    _post_one(up_ev, screen_x_end, screen_y_end, local_x_end, local_y_end)


def _post_scroll(
    *,
    quartz,
    pid: int,
    screen_x: float,
    screen_y: float,
    scroll_dx: int,
    scroll_dy: int,
    window_id: int = 0,
) -> None:
    scroll_event = quartz.CGEventCreateScrollWheelEvent(
        None,
        quartz.kCGScrollEventUnitLine,
        2,
        int(scroll_dy),
        int(scroll_dx),
    )
    quartz.CGEventSetLocation(
        scroll_event, quartz.CGPointMake(float(screen_x), float(screen_y)),
    )
    if window_id > 0:
        quartz.CGEventSetIntegerValueField(
            scroll_event, quartz.kCGMouseEventWindowUnderMousePointer, int(window_id),
        )
        quartz.CGEventSetIntegerValueField(
            scroll_event,
            quartz.kCGMouseEventWindowUnderMousePointerThatCanHandleThisEvent,
            int(window_id),
        )
    quartz.CGEventPostToPid(int(pid), scroll_event)


def _post_key(
    *,
    quartz,
    pid: int,
    keycode: int,
    modifier_flags: int,
) -> None:
    down = quartz.CGEventCreateKeyboardEvent(None, int(keycode), True)
    if modifier_flags:
        quartz.CGEventSetFlags(down, modifier_flags)
    quartz.CGEventPostToPid(int(pid), down)
    time.sleep(0.01)
    up = quartz.CGEventCreateKeyboardEvent(None, int(keycode), False)
    if modifier_flags:
        quartz.CGEventSetFlags(up, modifier_flags)
    quartz.CGEventPostToPid(int(pid), up)


def main() -> int:
    _ensure_macos()
    parser = argparse.ArgumentParser(description="Codex-style background click probe (macOS only)")
    parser.add_argument("--app", type=str, help="target app name or bundle id")
    parser.add_argument("--pid", type=int, help="target pid")
    parser.add_argument("--window-id", type=int, help="target CGWindowID")
    parser.add_argument("--x", type=float, help="window-local x")
    parser.add_argument("--y", type=float, help="window-local y")
    parser.add_argument("--screen-x", type=float, help="screen x (overrides --x)")
    parser.add_argument("--screen-y", type=float, help="screen y (overrides --y)")
    parser.add_argument("--click-count", type=int, default=1)
    parser.add_argument("--mouse-button", choices=("left", "right", "middle"), default="left")
    parser.add_argument("--subtype-value", type=int, default=3)
    parser.add_argument("--no-subtype", action="store_true")
    parser.add_argument("--no-window-fields", action="store_true")
    parser.add_argument("--no-mask-command-when-bg", action="store_true")
    parser.add_argument("--no-window-local", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--print-windows", action="store_true")
    parser.add_argument("--allow-negative-layer", action="store_true", help="allow choosing negative layer windows")
    parser.add_argument("--no-prefer-layer0", action="store_true", help="do not prioritize layer=0 windows")
    parser.add_argument("--min-alpha", type=float, default=0.05, help="minimum window alpha for candidate selection")
    parser.add_argument("--electron-mode", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--action", choices=("click", "move", "drag", "scroll", "key"), default="click",
                        help="event type to send")
    parser.add_argument("--keycode", type=int, help="virtual keycode for key action")
    parser.add_argument("--key-char", type=str,
                        help="key name for key action (a-z, 0-9, return, space, tab, escape, "
                             "left, right, up, down, f1-f12, delete, forwarddelete)")
    parser.add_argument("--modifiers", type=str, default="",
                        help="comma-separated modifier keys: command,shift,option,control")
    parser.add_argument("--scroll-dx", type=int, default=0, help="horizontal scroll delta (lines)")
    parser.add_argument("--scroll-dy", type=int, default=0,
                        help="vertical scroll delta (lines, negative=down)")
    parser.add_argument("--drag-to-x", type=float, help="window-local end x for drag")
    parser.add_argument("--drag-to-y", type=float, help="window-local end y for drag")
    parser.add_argument("--drag-to-screen-x", type=float, help="screen end x for drag")
    parser.add_argument("--drag-to-screen-y", type=float, help="screen end y for drag")
    parser.add_argument("--drag-steps", type=int, default=10,
                        help="number of intermediate move events for drag")
    args = parser.parse_args()

    import AppKit  # noqa: WPS433
    import Quartz  # noqa: WPS433
    import objc  # noqa: WPS433

    pid = _resolve_pid(AppKit, args.app, args.pid)
    action = args.action

    # Key events only need PID, no window targeting required
    if action == "key":
        keycode = args.keycode
        if keycode is None and args.key_char:
            keycode = KEYCODE_MAP.get(args.key_char.strip().lower())
            if keycode is None:
                raise SystemExit(f"unknown key name: {args.key_char}")
        if keycode is None:
            raise SystemExit("provide --keycode or --key-char for key action")
        modifier_flags = _resolve_modifier_flags(Quartz, args.modifiers)
        frontmost_before = _frontmost_app_name(AppKit)
        app_active_before = _app_is_active(AppKit, pid)
        payload = {
            "action": "key",
            "pid": int(pid),
            "keycode": keycode,
            "key_char": args.key_char or "",
            "modifiers": args.modifiers,
            "modifier_flags": modifier_flags,
            "frontmost_before": frontmost_before,
            "app_active_before": app_active_before,
            "dry_run": bool(args.dry_run),
        }
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        _post_key(quartz=Quartz, pid=pid, keycode=keycode, modifier_flags=modifier_flags)
        time.sleep(0.12)
        payload["frontmost_after"] = _frontmost_app_name(AppKit)
        payload["app_active_after"] = _app_is_active(AppKit, pid)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    candidates = _window_candidates_for_pid(Quartz, pid)
    target_window = _pick_window(
        candidates,
        args.window_id,
        exclude_negative_layer=not args.allow_negative_layer,
        prefer_layer0=not args.no_prefer_layer0,
        min_alpha=args.min_alpha,
    )
    inferred_electron = _infer_electron_app(AppKit, pid)
    is_electron = inferred_electron
    if args.electron_mode == "on":
        is_electron = True
    elif args.electron_mode == "off":
        is_electron = False

    if args.print_windows:
        print(
            json.dumps(
                [
                    {
                        "window_id": item.window_id,
                        "layer": item.layer,
                        "owner_name": item.owner_name,
                        "title": item.title,
                        "x": item.x,
                        "y": item.y,
                        "width": item.width,
                        "height": item.height,
                        "alpha": item.alpha,
                    }
                    for item in candidates
                ],
                ensure_ascii=False,
                indent=2,
            )
        )

    if args.screen_x is not None and args.screen_y is not None:
        screen_x = float(args.screen_x)
        screen_y = float(args.screen_y)
        local_x = screen_x - target_window.x
        local_y = screen_y - target_window.y
    else:
        local_x = float(args.x) if args.x is not None else (target_window.width * 0.5)
        local_y = float(args.y) if args.y is not None else (target_window.height * 0.5)
        screen_x = target_window.x + local_x
        screen_y = target_window.y + local_y

    # Resolve drag end coordinates
    end_screen_x = end_screen_y = end_local_x = end_local_y = 0.0
    if action == "drag":
        if args.drag_to_screen_x is not None and args.drag_to_screen_y is not None:
            end_screen_x = float(args.drag_to_screen_x)
            end_screen_y = float(args.drag_to_screen_y)
            end_local_x = end_screen_x - target_window.x
            end_local_y = end_screen_y - target_window.y
        elif args.drag_to_x is not None and args.drag_to_y is not None:
            end_local_x = float(args.drag_to_x)
            end_local_y = float(args.drag_to_y)
            end_screen_x = target_window.x + end_local_x
            end_screen_y = target_window.y + end_local_y
        else:
            raise SystemExit(
                "drag action requires --drag-to-x/--drag-to-y "
                "or --drag-to-screen-x/--drag-to-screen-y"
            )

    setter_window_location = _load_private_window_location_setter()
    frontmost_before = _frontmost_app_name(AppKit)
    app_active_before = _app_is_active(AppKit, pid)

    payload = {
        "action": action,
        "pid": int(pid),
        "window_id": int(target_window.window_id),
        "window_layer": int(target_window.layer),
        "window_alpha": float(target_window.alpha),
        "window_owner_name": target_window.owner_name,
        "window_title": target_window.title,
        "window_bounds": {
            "x": target_window.x,
            "y": target_window.y,
            "width": target_window.width,
            "height": target_window.height,
        },
        "screen_point": {"x": screen_x, "y": screen_y},
        "window_local_point": {"x": local_x, "y": local_y},
        "flags_mask_command_when_background": not args.no_mask_command_when_bg,
        "include_subtype": not args.no_subtype,
        "subtype_value": int(args.subtype_value),
        "include_window_fields": not args.no_window_fields,
        "include_window_local": not args.no_window_local,
        "has_private_CGEventSetWindowLocation": bool(setter_window_location is not None),
        "electron_inferred": bool(inferred_electron),
        "electron_mode": args.electron_mode,
        "electron_effective": bool(is_electron),
        "selection_policy": {
            "exclude_negative_layer": not args.allow_negative_layer,
            "prefer_layer0": not args.no_prefer_layer0,
            "min_alpha": args.min_alpha,
        },
        "frontmost_before": frontmost_before,
        "app_active_before": app_active_before,
        "dry_run": bool(args.dry_run),
    }

    if action == "drag":
        payload["drag_end_screen_point"] = {"x": end_screen_x, "y": end_screen_y}
        payload["drag_end_local_point"] = {"x": end_local_x, "y": end_local_y}
        payload["drag_steps"] = args.drag_steps
    if action == "scroll":
        payload["scroll_dx"] = args.scroll_dx
        payload["scroll_dy"] = args.scroll_dy

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    if action == "click":
        _post_click(
            quartz=Quartz,
            appkit=AppKit,
            objc_mod=objc,
            setter_window_location=setter_window_location,
            pid=pid,
            window_id=target_window.window_id,
            screen_x=screen_x,
            screen_y=screen_y,
            local_x=local_x,
            local_y=local_y,
            click_count=args.click_count,
            mouse_button=args.mouse_button,
            include_subtype=not args.no_subtype,
            subtype_value=args.subtype_value,
            include_window_fields=not args.no_window_fields,
            include_mask_command_when_bg=not args.no_mask_command_when_bg,
            include_window_local=not args.no_window_local,
        )
    elif action == "move":
        _post_move(
            quartz=Quartz,
            appkit=AppKit,
            objc_mod=objc,
            setter_window_location=setter_window_location,
            pid=pid,
            window_id=target_window.window_id,
            screen_x=screen_x,
            screen_y=screen_y,
            local_x=local_x,
            local_y=local_y,
            include_window_fields=not args.no_window_fields,
            include_window_local=not args.no_window_local,
        )
    elif action == "drag":
        _post_drag(
            quartz=Quartz,
            appkit=AppKit,
            objc_mod=objc,
            setter_window_location=setter_window_location,
            pid=pid,
            window_id=target_window.window_id,
            screen_x_start=screen_x,
            screen_y_start=screen_y,
            local_x_start=local_x,
            local_y_start=local_y,
            screen_x_end=end_screen_x,
            screen_y_end=end_screen_y,
            local_x_end=end_local_x,
            local_y_end=end_local_y,
            mouse_button=args.mouse_button,
            steps=args.drag_steps,
            include_subtype=not args.no_subtype,
            subtype_value=args.subtype_value,
            include_window_fields=not args.no_window_fields,
            include_mask_command_when_bg=not args.no_mask_command_when_bg,
            include_window_local=not args.no_window_local,
        )
    elif action == "scroll":
        _post_scroll(
            quartz=Quartz,
            pid=pid,
            screen_x=screen_x,
            screen_y=screen_y,
            scroll_dx=args.scroll_dx,
            scroll_dy=args.scroll_dy,
            window_id=target_window.window_id,
        )

    time.sleep(0.12)

    payload["frontmost_after"] = _frontmost_app_name(AppKit)
    payload["app_active_after"] = _app_is_active(AppKit, pid)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
