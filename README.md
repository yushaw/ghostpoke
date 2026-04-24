# ghostpoke

A macOS background event injection tool -- sends click/move/drag/scroll/key events to background windows without activating them.

## Demo

![Demo](ghostpoke-demo.mp4)

## Files

- `ghostpoke_probe.py`: Core probe supporting 5 background event types.
- `demo.py`: Automated demo script that launches the electron-echo fixture and showcases each event type.
- `electron-echo/`: Electron test app that displays received background events in real time.
- `pyproject.toml`: Dependency management (PyObjC).

## Setup

```bash
uv sync
```

## Quick start

```bash
# List parameters and windows (no events sent)
uv run python ghostpoke_probe.py --app "Finder" --dry-run --print-windows

# Background click
uv run python ghostpoke_probe.py --app "Finder" --allow-negative-layer

# Background keyboard -- send Cmd+T to Chrome
uv run python ghostpoke_probe.py --app "Google Chrome" --action key --key-char t --modifiers command

# Background scroll
uv run python ghostpoke_probe.py --app "Finder" --action scroll --scroll-dy -3

# Background drag
uv run python ghostpoke_probe.py --app "Finder" --action drag \
  --x 50 --y 50 --drag-to-x 300 --drag-to-y 300 --drag-steps 20

# Background mouse move
uv run python ghostpoke_probe.py --app "Finder" --action move --x 100 --y 200
```

## Demo script

Automated demo: launches electron-echo, injects all 5 event types in the background, and displays results in real time.

```bash
uv run python demo.py
uv run python demo.py --focus-app Terminal --speed 0.5
```

- click / key / drag are delivered via `CGEventPostToPid`
- move / scroll are delivered via CDP (`Input.dispatchMouseEvent`) to bypass Chromium limitations

## Supported actions

| Action | Description | Key args |
|--------|-------------|----------|
| click  | Mouse click (left/right/middle) | `--click-count`, `--mouse-button` |
| move   | Mouse move | `--x/--y` or `--screen-x/--screen-y` |
| drag   | Mouse drag | `--drag-to-x/--drag-to-y`, `--drag-steps` |
| scroll | Scroll wheel | `--scroll-dx`, `--scroll-dy` |
| key    | Keyboard input | `--key-char` or `--keycode`, `--modifiers` |

## Core path

Mouse events (click/move/drag):

1. `NSEvent.mouseEventWithType(...windowNumber...)`
2. `NSEvent -> CGEvent`
3. Write fields `3/7/91/92`
4. `CGEventSetLocation(screen)`
5. Optionally `CGEventSetWindowLocation(window-local)`
6. For background apps, optionally set `kCGEventFlagMaskCommand(0x00100000)`
7. `CGEventPostToPid(pid, event)`

Key/scroll events are delivered directly via `CGEventCreate*Event` + `CGEventPostToPid`.

## Chromium background event limitations

| Event type | CGEventPostToPid | Root cause |
|------------|-----------------|------------|
| click | OK | Routed through NSWindow.sendEvent -> responder chain |
| key | OK | Same as above |
| drag | OK | mouseDragged goes through sendEvent, bypasses TrackingArea |
| move | **blocked** | Chromium uses `NSTrackingActiveInActiveApp`; silently dropped when app is not foreground |
| scroll | **blocked** | AppKit routes by physical cursor position; dropped if cursor is not over the window |

Source: `components/remote_cocoa/app_shim/bridged_content_view.mm`

Workaround: CDP `Input.dispatchMouseEvent` injects directly into the Blink rendering pipeline (implemented in demo.py).

## Window selection

By default, windows with negative layer are excluded; layer=0 windows are preferred; low-opacity windows are filtered (`--min-alpha 0.05`).
To include negative-layer windows: `--allow-negative-layer`.
