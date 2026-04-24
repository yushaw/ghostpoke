# ghostpoke

macOS 后台事件投递工具 -- 向后台窗口发送 click/move/drag/scroll/key 事件且不激活目标窗口。

## Files

- `ghostpoke_probe.py`: 核心探针，支持 5 种后台事件类型。
- `demo.py`: 自动化演示脚本，启动 electron-echo fixture 并逐一展示每种事件。
- `electron-echo/`: Electron 测试应用，实时展示收到的后台事件。
- `pyproject.toml`: 依赖管理（PyObjC）。

## Setup

```bash
uv sync
```

## Quick start

```bash
# 查看参数与窗口（不发事件）
uv run python ghostpoke_probe.py --app "Finder" --dry-run --print-windows

# 后台点击
uv run python ghostpoke_probe.py --app "Finder" --allow-negative-layer

# 后台键盘 -- 给 Chrome 发 Cmd+T
uv run python ghostpoke_probe.py --app "Google Chrome" --action key --key-char t --modifiers command

# 后台滚动
uv run python ghostpoke_probe.py --app "Finder" --action scroll --scroll-dy -3

# 后台拖拽
uv run python ghostpoke_probe.py --app "Finder" --action drag \
  --x 50 --y 50 --drag-to-x 300 --drag-to-y 300 --drag-steps 20

# 后台鼠标移动
uv run python ghostpoke_probe.py --app "Finder" --action move --x 100 --y 200
```

## Demo

自动化演示：启动 electron-echo，后台逐一注入 5 种事件，窗口实时展示效果。

```bash
uv run python demo.py
uv run python demo.py --focus-app Terminal --speed 0.5
```

- click / key / drag 通过 `CGEventPostToPid` 投递
- move / scroll 通过 CDP (`Input.dispatchMouseEvent`) 绕过 Chromium 限制

## Supported actions

| Action | Description | Key args |
|--------|-------------|----------|
| click  | 鼠标点击 (left/right/middle) | `--click-count`, `--mouse-button` |
| move   | 鼠标移动 | `--x/--y` or `--screen-x/--screen-y` |
| drag   | 鼠标拖拽 | `--drag-to-x/--drag-to-y`, `--drag-steps` |
| scroll | 滚轮滚动 | `--scroll-dx`, `--scroll-dy` |
| key    | 键盘按键 | `--key-char` or `--keycode`, `--modifiers` |

## Core path

Mouse 事件 (click/move/drag):

1. `NSEvent.mouseEventWithType(...windowNumber...)`
2. `NSEvent -> CGEvent`
3. 写字段 `3/7/91/92`
4. `CGEventSetLocation(screen)`
5. 可选 `CGEventSetWindowLocation(window-local)`
6. 后台 app 时可加 `kCGEventFlagMaskCommand(0x00100000)`
7. `CGEventPostToPid(pid, event)`

Key/scroll 直接通过 `CGEventCreate*Event` + `CGEventPostToPid` 投递。

## Chromium 后台事件限制

| Event type | CGEventPostToPid | Root cause |
|------------|-----------------|------------|
| click | OK | 走 NSWindow.sendEvent -> responder chain |
| key | OK | 同上 |
| drag | OK | mouseDragged 走 sendEvent，不经 TrackingArea |
| move | **blocked** | Chromium 用 `NSTrackingActiveInActiveApp`，非前台 app 静默丢弃 |
| scroll | **blocked** | AppKit 按光标物理位置路由，光标不在窗口上方则丢弃 |

源码: `components/remote_cocoa/app_shim/bridged_content_view.mm`

绕过方案: CDP `Input.dispatchMouseEvent` 直接注入 Blink 渲染管线 (demo.py 中已实现)。

## Window selection

默认排除负 layer、优先 layer=0、过滤低透明度窗口 (`--min-alpha 0.05`)。
放开限制: `--allow-negative-layer`。