"""
Function executor for Deepgram Voice Agent desktop control.

Maps function names from Deepgram's FunctionCallRequest frames to
local OS operations. All synchronous OS calls are wrapped in
asyncio.to_thread() to avoid blocking the voice audio stream.
"""

import asyncio
import os
import platform
import shlex
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

# ─── Platform helpers ──────────────────────────────────────────────────

IS_WINDOWS = platform.system() == "Windows"
IS_MACOS = platform.system() == "Darwin"
IS_LINUX = platform.system() == "Linux"


def _safe_print(*args, **kwargs) -> None:
    """Console-safe print for Windows cp1252 terminals.

    Windows consoles default to the cp1252 codec, which cannot encode emoji or
    other non-Latin-1 characters.  A bare ``print()`` of such text raises
    ``UnicodeEncodeError`` ('charmap' codec error) and can kill the calling
    thread — which is exactly what crashed the vision stream thread and made
    ``vision_stream_start`` report an error.  This helper re-encodes with the
    console's own codec using ``errors="replace"`` (preserving encodable
    characters like ``°C`` or accents, replacing only emoji), so logging
    never crashes the stream.
    """
    import builtins
    import sys
    try:
        builtins.print(*args, **kwargs)
    except UnicodeEncodeError:
        # First pass: re-encode with the console's actual codec (preserves
        # encodable characters like °C or accents).  If the output still
        # can't be encoded (e.g. the console is cp1252 but stdout reports
        # utf-8), fall back to ASCII replacement — never raise.
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        try:
            safe = [
                a.encode(enc, errors="replace").decode(enc)
                if isinstance(a, str) else a
                for a in args
            ]
            builtins.print(*safe, **kwargs)
        except UnicodeEncodeError:
            safe = [
                a.encode("ascii", errors="replace").decode("ascii")
                if isinstance(a, str) else a
                for a in args
            ]
            builtins.print(*safe, **kwargs)


# ─── OS Operation Implementations (blocking — called via to_thread) ────

def _minimize_window(window_name: str | None = None) -> dict[str, Any]:
    """Minimize the active window or a named window."""
    if IS_WINDOWS:
        import pygetwindow as gw
        if window_name:
            wins = gw.getWindowsWithTitle(window_name)
            if wins:
                wins[0].minimize()
                return {"status": "success", "detail": f"Minimized '{window_name}'"}
            return {"status": "error", "detail": f"No window found with title '{window_name}'"}
        else:
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.ShowWindow(user32.GetForegroundWindow(), 6)  # SW_MINIMIZE
            return {"status": "success", "detail": "Active window minimized"}
    elif IS_MACOS:
        if window_name:
            subprocess.run(["osascript", "-e",
                f'tell application "{window_name}" to set minimized of windows to true'],
                capture_output=True)
        else:
            subprocess.run(["osascript", "-e",
                'tell application "System Events" to keystroke "m" using command down'],
                capture_output=True)
        return {"status": "success", "detail": "Window minimized"}
    else:
        # Linux — use wmctrl
        if window_name:
            subprocess.run(["wmctrl", "-r", window_name, "-b", "add,hidden"],
                           capture_output=True)
        else:
            subprocess.run(["xdotool", "getactivewindow", "windowminimize"],
                           capture_output=True)
        return {"status": "success", "detail": "Window minimized"}


def _maximize_window(window_name: str | None = None) -> dict[str, Any]:
    """Maximize the active window or a named window."""
    if IS_WINDOWS:
        import pygetwindow as gw
        if window_name:
            wins = gw.getWindowsWithTitle(window_name)
            if wins:
                wins[0].maximize()
                return {"status": "success", "detail": f"Maximized '{window_name}'"}
            return {"status": "error", "detail": f"No window found with title '{window_name}'"}
        else:
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            user32.ShowWindow(user32.GetForegroundWindow(), 3)  # SW_MAXIMIZE
            return {"status": "success", "detail": "Active window maximized"}
    elif IS_MACOS:
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to keystroke "m" using {command down, option down}'],
            capture_output=True)
        return {"status": "success", "detail": "Window maximized"}
    else:
        if window_name:
            subprocess.run(["wmctrl", "-r", window_name, "-b", "remove,hidden"],
                           capture_output=True)
        subprocess.run(["xdotool", "getactivewindow", "windowmaximize"],
                       capture_output=True)
        return {"status": "success", "detail": "Window maximized"}


def _open_file(file_path: str) -> dict[str, Any]:
    """Open a file or application using the default OS handler."""
    path = Path(file_path).expanduser().resolve()
    if not path.exists():
        return {"status": "error", "detail": f"Path not found: {path}"}
    try:
        if IS_WINDOWS:
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif IS_MACOS:
            subprocess.run(["open", str(path)], check=True)
        else:
            subprocess.run(["xdg-open", str(path)], check=True)
        return {"status": "success", "detail": f"Opened {path.name}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _launch_app(app_name_or_path: str) -> dict[str, Any]:
    """Launch an application by name or path."""
    try:
        if IS_WINDOWS:
            subprocess.Popen(["cmd", "/c", "start", "", app_name_or_path],
                             shell=False)
        elif IS_MACOS:
            subprocess.run(["open", "-a", app_name_or_path], check=True)
        else:
            subprocess.Popen([app_name_or_path], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
        return {"status": "success", "detail": f"Launched {app_name_or_path}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _close_app(app_name: str) -> dict[str, Any]:
    """Close an application by name."""
    try:
        if IS_WINDOWS:
            subprocess.run(["taskkill", "/f", "/im", f"{app_name}.exe"],
                           capture_output=True, timeout=5)
        elif IS_MACOS:
            subprocess.run(["pkill", "-f", app_name], capture_output=True, timeout=5)
        else:
            subprocess.run(["pkill", "-f", app_name], capture_output=True, timeout=5)
        return {"status": "success", "detail": f"Closed {app_name}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "detail": f"Timed out closing {app_name}"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _get_system_status() -> dict[str, Any]:
    """Get current system status (CPU, memory, disk)."""
    try:
        import psutil
        cpu = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        return {
            "status": "success",
            "cpu_percent": cpu,
            "memory_percent": mem.percent,
            "memory_used_gb": round(mem.used / (1024**3), 1),
            "memory_total_gb": round(mem.total / (1024**3), 1),
            "disk_percent": disk.percent,
            "disk_free_gb": round(disk.free / (1024**3), 1),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _get_hardware_status(detailed: bool = False) -> dict[str, Any]:
    """Get comprehensive hardware status (CPU, RAM, GPU, disk, network, uptime).

    Args:
        detailed: If True, returns full telemetry with GPU details.
                  If False, returns a lightweight summary.

    Returns:
        Dict with hardware telemetry and a human-readable summary.
    """
    try:
        from system_control.hardware_monitor import (
            get_hardware_monitor,
            format_hardware_summary,
            format_uptime,
        )
        monitor = get_hardware_monitor()
        snap = monitor.snapshot(collect_processes=detailed)
        return {
            "status": "success",
            "telemetry": snap.to_dict() if detailed else snap.to_brief(),
            "summary": format_hardware_summary(snap),
            "uptime": format_uptime(snap.uptime_seconds),
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _start_hardware_monitoring(interval: float = 5.0) -> dict[str, Any]:
    """Start background hardware monitoring with threshold alerts.

    Args:
        interval: Seconds between telemetry snapshots (default 5.0).

    Returns:
        Dict with monitoring start result.
    """
    try:
        from system_control.hardware_monitor import get_hardware_monitor
        import asyncio
        monitor = get_hardware_monitor()
        try:
            asyncio.run(monitor.start_monitoring(interval=interval))
        except RuntimeError:
            # Already in a running loop — schedule as task
            import asyncio as _asyncio
            try:
                _asyncio.get_event_loop().create_task(monitor.start_monitoring(interval=interval))
            except RuntimeError:
                asyncio.run(monitor.start_monitoring(interval=interval))
        return {
            "status": "success",
            "detail": f"Hardware monitoring started (interval={interval}s)",
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _stop_hardware_monitoring() -> dict[str, Any]:
    """Stop background hardware monitoring."""
    try:
        from system_control.hardware_monitor import get_hardware_monitor
        import asyncio
        monitor = get_hardware_monitor()
        try:
            asyncio.run(monitor.stop_monitoring())
        except RuntimeError:
            import asyncio as _asyncio
            try:
                _asyncio.get_event_loop().create_task(monitor.stop_monitoring())
            except RuntimeError:
                asyncio.run(monitor.stop_monitoring())
        return {"status": "success", "detail": "Hardware monitoring stopped"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _get_hardware_alerts() -> dict[str, Any]:
    """Get recent hardware threshold alerts."""
    try:
        from system_control.hardware_monitor import get_hardware_monitor
        monitor = get_hardware_monitor()
        alerts = monitor.get_alerts()
        if alerts:
            return {
                "status": "success",
                "alerts": alerts,
                "count": len(alerts),
                "detail": " | ".join(a["message"] for a in alerts[-3:]),
            }
        return {"status": "success", "alerts": [], "count": 0, "detail": "No active alerts — all systems nominal"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _list_files(directory: str, pattern: str = "*") -> dict[str, Any]:
    """List files in a directory with optional glob pattern."""
    path = Path(directory).expanduser().resolve()
    if not path.is_dir():
        return {"status": "error", "detail": f"Directory not found: {path}"}
    try:
        files = list(path.glob(pattern))
        entries = []
        for f in sorted(files):
            entries.append({
                "name": f.name,
                "is_dir": f.is_dir(),
                "size_bytes": f.stat().st_size if f.is_file() else 0,
            })
        return {
            "status": "success",
            "directory": str(path),
            "total": len(entries),
            "files": entries[:50],  # limit to first 50
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _run_shell_command(command: str) -> dict[str, Any]:
    """Run a shell command and return output. SAFE: uses shlex.split, no shell=True."""
    try:
        args = shlex.split(command)
        # Use errors='replace' to handle non-ASCII characters (weather symbols,
        # emoji, etc.) that can't be decoded with the system's cp1252 codec
        result = subprocess.run(
            args, capture_output=True, text=True,
            errors='replace', timeout=30
        )
        return {
            "status": "success",
            "stdout": result.stdout[:2000],
            "stderr": result.stderr[:500],
            "return_code": result.returncode,
        }
    except FileNotFoundError:
        return {"status": "error", "detail": f"Command not found: {args[0] if args else command}"}
    except subprocess.TimeoutExpired:
        return {"status": "error", "detail": "Command timed out (30s limit)"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _take_screenshot(file_path: str = "") -> dict[str, Any]:
    """Take a screenshot and save it to a file.

    Args:
        file_path: Optional path to save the screenshot. If empty, saves to a temp file.

    Returns:
        Dict with path to saved screenshot.
    """
    try:
        from PIL import ImageGrab

        save_path = file_path.strip() if file_path.strip() else ""
        if not save_path:
            timestamp = int(time.time())
            save_path = str(Path(tempfile.gettempdir()) / f"barq_screenshot_{timestamp}.png")
        else:
            save_path = str(Path(save_path).expanduser().resolve())

        # Ensure parent directory exists
        Path(save_path).parent.mkdir(parents=True, exist_ok=True)

        # Grab the full screen
        screenshot = ImageGrab.grab(all_screens=True)
        screenshot.save(save_path, "PNG")

        width, height = screenshot.size
        return {
            "status": "success",
            "file_path": save_path,
            "width": width,
            "height": height,
            "detail": f"Screenshot saved to {save_path} ({width}x{height})",
        }
    except ImportError:
        return {"status": "error", "detail": "Pillow (PIL) is not installed. Install with: pip install Pillow"}
    except Exception as e:
        return {"status": "error", "detail": f"Screenshot failed: {e}"}


def _clipboard_op(action: str = "read", text: str = "") -> dict[str, Any]:
    """Read from or write to the system clipboard.

    Args:
        action: "read" to get clipboard contents, "write" to set clipboard contents.
        text: Text to write to clipboard (only used when action="write").

    Returns:
        Dict with clipboard content (on read) or confirmation (on write).
    """
    try:
        import pyperclip

        if action == "write":
            if not text:
                return {"status": "error", "detail": "No text provided for clipboard write"}
            pyperclip.copy(text)
            return {"status": "success", "detail": f"Copied '{text[:50]}{'...' if len(text) > 50 else ''}' to clipboard"}

        # Default: read clipboard
        content = pyperclip.paste()
        truncated = len(content) > 1000
        return {
            "status": "success",
            "action": "read",
            "content": content[:1000] if truncated else content,
            "truncated": truncated,
            "length": len(content),
            "detail": f"Clipboard contains {len(content)} characters" if content else "Clipboard is empty",
        }
    except ImportError:
        return {"status": "error", "detail": "pyperclip is not installed. Install with: pip install pyperclip"}
    except Exception as e:
        return {"status": "error", "detail": f"Clipboard operation failed: {e}"}


def _focus_window(window_name: str) -> dict[str, Any]:
    """Bring a specific window to the foreground / give it focus.

    Args:
        window_name: Name or title substring of the window to focus.

    Returns:
        Dict with focus result.
    """
    if not window_name:
        return {"status": "error", "detail": "window_name is required"}

    if IS_WINDOWS:
        try:
            import pygetwindow as gw
            wins = gw.getWindowsWithTitle(window_name)
            if not wins:
                return {"status": "error", "detail": f"No window found with title containing '{window_name}'"}
            wins[0].activate()
            return {"status": "success", "detail": f"Focused '{window_name}'"}
        except ImportError:
            # Fallback: use ctypes to enumerate windows
            try:
                import ctypes
                user32 = ctypes.windll.user32  # type: ignore[attr-defined]
                # Find window by title using FindWindowW
                handle = user32.FindWindowW(None, window_name)
                if handle:
                    user32.SetForegroundWindow(handle)
                    return {"status": "success", "detail": f"Focused '{window_name}'"}
                else:
                    return {"status": "error", "detail": f"No window found with title '{window_name}'"}
            except Exception as e2:
                return {"status": "error", "detail": f"Focus window failed: {e2}"}
        except Exception as e:
            return {"status": "error", "detail": f"Focus window failed: {e}"}
    elif IS_MACOS:
        try:
            subprocess.run(["osascript", "-e",
                f'tell application "{window_name}" to activate'],
                capture_output=True, timeout=5, check=False)
            return {"status": "success", "detail": f"Focused '{window_name}'"}
        except Exception as e:
            return {"status": "error", "detail": f"Focus window failed: {e}"}
    else:
        # Linux — use wmctrl
        try:
            result = subprocess.run(["wmctrl", "-a", window_name],
                                    capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return {"status": "success", "detail": f"Focused '{window_name}'"}
            else:
                return {"status": "error", "detail": f"Window not found: {result.stderr.strip()}"}
        except FileNotFoundError:
            return {"status": "error", "detail": "wmctrl not installed. Install with: apt install wmctrl"}
        except Exception as e:
            return {"status": "error", "detail": f"Focus window failed: {e}"}


def _set_app_volume(app_name: str = "", level: int = 50) -> dict[str, Any]:
    """Set the volume level for a specific application or the system master volume.

    Args:
        app_name: Name of the application. If empty, sets master/system volume.
        level: Volume level from 0 (mute) to 100 (max). Default 50.

    Returns:
        Dict with volume change result.
    """
    # Clamp level to 0-100
    level = max(0, min(100, level))

    if IS_WINDOWS:
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))

            if app_name:
                # Get all audio sessions and find the matching app
                sessions = AudioUtilities.GetAllSessions()
                for session in sessions:
                    if session.Process and session.Process.name().lower() == app_name.lower():
                        session.SimpleAudioVolume.SetMasterVolume(level / 100.0, None)
                        return {"status": "success", "detail": f"Set '{app_name}' volume to {level}%"}
                return {"status": "error", "detail": f"No audio session found for '{app_name}'"}
            else:
                # Set master volume
                volume.SetMasterVolumeLevelScalar(level / 100.0, None)
                return {"status": "success", "detail": f"Master volume set to {level}%"}

        except ImportError:
            # Fallback: use powershell
            if app_name:
                # Per-app volume via powershell requires more complex handling
                return {"status": "error", "detail": "Per-app volume control requires pycaw. Install: pip install pycaw"}
            else:
                subprocess.run([
                    "powershell", "-c",
                    "(New-Object -ComObject WScript.Shell).SendKeys([char]174)"
                ], capture_output=True, timeout=5)
                return {"status": "success", "detail": "Master volume set approximately"}
        except Exception as e:
            return {"status": "error", "detail": f"Volume control failed: {e}"}

    elif IS_MACOS:
        try:
            # macOS uses 0-100 range for volume
            subprocess.run(["osascript", "-e",
                f'set volume output volume {level}'],
                capture_output=True, timeout=5)
            return {"status": "success", "detail": f"Volume set to {level}%"}
        except Exception as e:
            return {"status": "error", "detail": f"Volume control failed: {e}"}
    else:
        # Linux — use amixer or pactl
        try:
            # Convert 0-100 to 0-65535 for amixer
            amixer_level = int(level / 100 * 65535)
            subprocess.run(["amixer", "sset", "Master", f"{amixer_level}"],
                           capture_output=True, timeout=5)
            return {"status": "success", "detail": f"Volume set to {level}%"}
        except FileNotFoundError:
            try:
                subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{level}%"],
                               capture_output=True, timeout=5)
                return {"status": "success", "detail": f"Volume set to {level}%"}
            except Exception as e2:
                return {"status": "error", "detail": f"Volume control failed: {e2}"}
        except Exception as e:
            return {"status": "error", "detail": f"Volume control failed: {e}"}


def _mute_volume(mute: bool = True) -> dict[str, Any]:
    """Mute or unmute the system volume.

    Args:
        mute: True to mute, False to unmute.

    Returns:
        Dict with mute result.
    """
    if IS_WINDOWS:
        try:
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL

            devices = AudioUtilities.GetSpeakers()
            interface = devices.Activate(
                IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            volume = cast(interface, POINTER(IAudioEndpointVolume))

            volume.SetMute(1 if mute else 0, None)
            state = "muted" if mute else "unmuted"
            return {"status": "success", "detail": f"System volume {state}"}

        except ImportError:
            # Fallback: VK_VOLUME_MUTE virtual key
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            # Simulate mute key press
            VK_VOLUME_MUTE = 0xAD
            user32.keybd_event(VK_VOLUME_MUTE, 0, 0, 0)  # Key down
            user32.keybd_event(VK_VOLUME_MUTE, 0, 2, 0)  # Key up
            return {"status": "success", "detail": "Volume toggled mute"}
        except Exception as e:
            return {"status": "error", "detail": f"Mute failed: {e}"}
    elif IS_MACOS:
        try:
            muted_str = "true" if mute else "false"
            subprocess.run(["osascript", "-e",
                f'set volume output muted {muted_str}'],
                capture_output=True, timeout=5)
            state = "muted" if mute else "unmuted"
            return {"status": "success", "detail": f"System volume {state}"}
        except Exception as e:
            return {"status": "error", "detail": f"Mute failed: {e}"}
    else:
        # Linux — use amixer
        try:
            if mute:
                subprocess.run(["amixer", "sset", "Master", "mute"],
                               capture_output=True, timeout=5)
            else:
                subprocess.run(["amixer", "sset", "Master", "unmute"],
                               capture_output=True, timeout=5)
            state = "muted" if mute else "unmuted"
            return {"status": "success", "detail": f"System volume {state}"}
        except FileNotFoundError:
            try:
                if mute:
                    subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "1"],
                                   capture_output=True, timeout=5)
                else:
                    subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"],
                                   capture_output=True, timeout=5)
                state = "muted" if mute else "unmuted"
                return {"status": "success", "detail": f"System volume {state}"}
            except Exception as e2:
                return {"status": "error", "detail": f"Mute failed: {e2}"}
        except Exception as e:
            return {"status": "error", "detail": f"Mute failed: {e}"}


def _media_control(action: str = "play_pause") -> dict[str, Any]:
    """Control media playback (play/pause, next, previous).

    Args:
        action: One of "play_pause", "next", "previous".

    Returns:
        Dict with media control result.
    """
    valid_actions = {"play_pause", "next", "previous"}
    if action not in valid_actions:
        return {"status": "error", "detail": f"Invalid action '{action}'. Must be one of: {', '.join(sorted(valid_actions))}"}

    if IS_WINDOWS:
        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]

        # Virtual key codes for media controls
        VK_MEDIA_PLAY_PAUSE = 0xB3
        VK_MEDIA_NEXT_TRACK = 0xB0
        VK_MEDIA_PREV_TRACK = 0xB1

        key_map = {
            "play_pause": VK_MEDIA_PLAY_PAUSE,
            "next": VK_MEDIA_NEXT_TRACK,
            "previous": VK_MEDIA_PREV_TRACK,
        }

        vk_code = key_map[action]
        user32.keybd_event(vk_code, 0, 0, 0)  # Key down
        user32.keybd_event(vk_code, 0, 2, 0)  # Key up

        action_labels = {
            "play_pause": "Play/Pause toggled",
            "next": "Next track",
            "previous": "Previous track",
        }
        return {"status": "success", "detail": action_labels[action]}

    elif IS_MACOS:
        # AppleScript keystroke uses key names, not numeric codes
        mac_key_map = {
            "play_pause": ("space",),
            "next": ("right", "command"),
            "previous": ("left", "command"),
        }
        key, *mods = mac_key_map[action]
        mods_clause = f" using {{{', '.join(mods)} down}}" if mods else ""
        subprocess.run(["osascript", "-e",
            f'tell application "System Events" to keystroke "{key}"{mods_clause}'],
            capture_output=True, timeout=5)
        return {"status": "success", "detail": f"Media: {action}"}

    else:
        # Linux — use playerctl (most media players support it)
        try:
            cmd_map = {
                "play_pause": "play-pause",
                "next": "next",
                "previous": "previous",
            }
            subprocess.run(["playerctl", cmd_map[action]],
                           capture_output=True, timeout=5)
            return {"status": "success", "detail": f"Media: {action}"}
        except FileNotFoundError:
            return {"status": "error", "detail": "playerctl not installed. Install with: apt install playerctl"}
        except Exception as e:
            return {"status": "error", "detail": f"Media control failed: {e}"}


def _empty_trash() -> dict[str, Any]:
    """Empty the system trash / recycle bin.

    Returns:
        Dict with trash result.
    """
    if IS_WINDOWS:
        try:
            # Use SHEmptyRecycleBinW via ctypes
            import ctypes
            shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
            # SHERB_NOCONFIRMATION = 0x1, SHERB_NOPROGRESSUI = 0x2, SHERB_NOSOUND = 0x4
            flags = 0x1 | 0x2 | 0x4  # No confirmation, no progress UI, no sound
            result = shell32.SHEmptyRecycleBinW(None, None, flags)
            if result == 0:  # S_OK
                return {"status": "success", "detail": "Recycle bin emptied"}
            else:
                return {"status": "error", "detail": f"Failed to empty recycle bin (code: {result})"}
        except Exception as e:
            return {"status": "error", "detail": f"Empty trash failed: {e}"}
    elif IS_MACOS:
        try:
            subprocess.run(["osascript", "-e",
                'tell application "Finder" to empty trash'],
                capture_output=True, timeout=30)
            return {"status": "success", "detail": "Trash emptied"}
        except Exception as e:
            return {"status": "error", "detail": f"Empty trash failed: {e}"}
    else:
        # Linux — trash-cli
        try:
            subprocess.run(["trash-empty"], capture_output=True, timeout=30)
            return {"status": "success", "detail": "Trash emptied"}
        except FileNotFoundError:
            return {"status": "error", "detail": "trash-cli not installed. Install with: apt install trash-cli"}
        except Exception as e:
            return {"status": "error", "detail": f"Empty trash failed: {e}"}


def _lock_screen() -> dict[str, Any]:
    """Lock the computer screen.

    Returns:
        Dict with lock screen result.
    """
    if IS_WINDOWS:
        try:
            import ctypes
            user32 = ctypes.windll.user32  # type: ignore[attr-defined]
            # LockWorkStation locks the screen
            result = user32.LockWorkStation()
            if result:
                return {"status": "success", "detail": "Screen locked"}
            else:
                return {"status": "error", "detail": "Failed to lock screen"}
        except Exception as e:
            return {"status": "error", "detail": f"Lock screen failed: {e}"}
    elif IS_MACOS:
        try:
            # Use the login window to lock the screen
            subprocess.run(["osascript", "-e",
                'tell application "System Events" to keystroke "q" using {command down, control down}'],
                capture_output=True, timeout=5)
            return {"status": "success", "detail": "Screen locked"}
        except Exception as e:
            return {"status": "error", "detail": f"Lock screen failed: {e}"}
    else:
        # Linux — use gnome-screensaver-command, xscreensaver, or loginctl
        try:
            # Try loginctl (systemd) first
            result = subprocess.run(["loginctl", "lock-session"],
                                    capture_output=True, timeout=5)
            if result.returncode == 0:
                return {"status": "success", "detail": "Screen locked"}
        except FileNotFoundError:
            pass

        try:
            subprocess.run(["gnome-screensaver-command", "-l"],
                           capture_output=True, timeout=5)
            return {"status": "success", "detail": "Screen locked"}
        except FileNotFoundError:
            pass

        try:
            subprocess.run(["xdg-screensaver", "lock"],
                           capture_output=True, timeout=5)
            return {"status": "success", "detail": "Screen locked"}
        except Exception:
            return {"status": "error", "detail": "Screen lock tools not found. Install: gnome-screensaver, xscreensaver, or xdg-utils"}


# ─── Mouse & Keyboard Control Functions (like Mark-L's computer_control.py) ─
# Requires: pip install pyautogui


def _mouse_click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
) -> dict[str, Any]:
    """Click at specified screen coordinates or current cursor position.

    Args:
        x: Optional X coordinate. If omitted, clicks at current position.
        y: Optional Y coordinate. If omitted, clicks at current position.
        button: "left", "right", or "middle". Default "left".
        clicks: Number of clicks (1=single, 2=double). Default 1.

    Returns:
        Dict with click result.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05

        if x is not None and y is not None:
            pyautogui.click(x, y, button=button, clicks=clicks)
            label = "Double-clicked" if clicks == 2 else "Clicked"
            return {"status": "success", "detail": f"{label} ({x}, {y}) [{button}]"}
        else:
            pyautogui.click(button=button, clicks=clicks)
            label = "Double-clicked" if clicks == 2 else "Clicked"
            return {"status": "success", "detail": f"{label} at current position [{button}]"}
    except ImportError:
        return {"status": "error", "detail": "PyAutoGUI not installed. Install with: pip install pyautogui"}
    except Exception as e:
        return {"status": "error", "detail": f"Mouse click failed: {e}"}


def _mouse_move(x: int, y: int, duration: float = 0.3) -> dict[str, Any]:
    """Move the mouse cursor to absolute screen coordinates.

    Args:
        x: Target X coordinate.
        y: Target Y coordinate.
        duration: Animation duration in seconds. Default 0.3.

    Returns:
        Dict with move result.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(x, y, duration=duration)
        return {"status": "success", "detail": f"Mouse moved to ({x}, {y})"}
    except ImportError:
        return {"status": "error", "detail": "PyAutoGUI not installed. Install with: pip install pyautogui"}
    except Exception as e:
        return {"status": "error", "detail": f"Mouse move failed: {e}"}


def _mouse_drag(
    x1: int, y1: int, x2: int, y2: int, duration: float = 0.5,
) -> dict[str, Any]:
    """Click-drag from one point to another.

    Args:
        x1: Starting X coordinate.
        y1: Starting Y coordinate.
        x2: Ending X coordinate.
        y2: Ending Y coordinate.
        duration: Drag duration in seconds. Default 0.5.

    Returns:
        Dict with drag result.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(x1, y1, duration=0.2)
        pyautogui.dragTo(x2, y2, duration=duration, button="left")
        return {"status": "success", "detail": f"Dragged from ({x1},{y1}) to ({x2},{y2})"}
    except ImportError:
        return {"status": "error", "detail": "PyAutoGUI not installed. Install with: pip install pyautogui"}
    except Exception as e:
        return {"status": "error", "detail": f"Mouse drag failed: {e}"}


def _mouse_scroll(direction: str = "down", amount: int = 3) -> dict[str, Any]:
    """Scroll the mouse wheel.

    Args:
        direction: "up", "down", "left", or "right".
        amount: Number of scroll clicks. Default 3.

    Returns:
        Dict with scroll result.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True

        vertical = direction in ("up", "down")
        clicks = amount if direction in ("up", "right") else -amount

        if vertical:
            pyautogui.scroll(clicks)
        else:
            pyautogui.hscroll(clicks)

        return {"status": "success", "detail": f"Scrolled {direction} x{amount}"}
    except ImportError:
        return {"status": "error", "detail": "PyAutoGUI not installed. Install with: pip install pyautogui"}
    except Exception as e:
        return {"status": "error", "detail": f"Scroll failed: {e}"}


def _keyboard_type(text: str, interval: float = 0.03) -> dict[str, Any]:
    """Type text at the current cursor position.

    Args:
        text: The text to type.
        interval: Seconds between keystrokes. Default 0.03.

    Returns:
        Dict with type result.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.typewrite(text, interval=interval)
        truncated = text[:60] + "..." if len(text) > 60 else text
        return {"status": "success", "detail": f"Typed: {truncated}"}
    except ImportError:
        return {"status": "error", "detail": "PyAutoGUI not installed. Install with: pip install pyautogui"}
    except Exception as e:
        return {"status": "error", "detail": f"Keyboard type failed: {e}"}


def _keyboard_smart_type(text: str, clear_first: bool = True) -> dict[str, Any]:
    """Type text smartly — clear the field first, then type via clipboard paste for long text.

    For longer text (>20 chars), uses clipboard paste which is faster and more
    reliable than individual keystrokes.

    Args:
        text: The text to type.
        clear_first: Whether to select-all + delete first. Default True.

    Returns:
        Dict with type result.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05

        if clear_first:
            modifier = "command" if IS_MACOS else "ctrl"
            pyautogui.hotkey(modifier, "a")
            pyautogui.press("delete")

        if len(text) > 20:
            # Use clipboard paste for long text (faster, fewer key events)
            try:
                import pyperclip
                pyperclip.copy(text)
                modifier = "command" if IS_MACOS else "ctrl"
                pyautogui.hotkey(modifier, "v")
                truncated = text[:60] + "..." if len(text) > 60 else text
                return {"status": "success", "detail": f"Smart-typed (clipboard): {truncated}"}
            except ImportError:
                pass  # Fall through to regular typewrite

        pyautogui.typewrite(text, interval=0.04)
        truncated = text[:60] + "..." if len(text) > 60 else text
        return {"status": "success", "detail": f"Smart-typed: {truncated}"}
    except ImportError:
        return {"status": "error", "detail": "PyAutoGUI not installed. Install with: pip install pyautogui"}
    except Exception as e:
        return {"status": "error", "detail": f"Smart type failed: {e}"}


def _keyboard_hotkey(keys: list[str]) -> dict[str, Any]:
    """Press a keyboard shortcut (e.g. ctrl+c, alt+tab, ctrl+shift+esc).

    Args:
        keys: List of key names like ["ctrl", "c"] for copy, ["alt", "tab"] for window switch.

    Returns:
        Dict with hotkey result.
    """
    if not keys:
        return {"status": "error", "detail": "At least one key is required"}
    key_list = keys if isinstance(keys, (list, tuple)) else [keys]
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.hotkey(*key_list)
        return {"status": "success", "detail": f"Hotkey: {'+'.join(key_list)}"}
    except ImportError:
        return {"status": "error", "detail": "PyAutoGUI not installed. Install with: pip install pyautogui"}
    except Exception as e:
        return {"status": "error", "detail": f"Hotkey failed: {e}"}


def _keyboard_press(key: str = "enter") -> dict[str, Any]:
    """Press a single keyboard key (e.g. enter, tab, escape, space, backspace).

    Args:
        key: Name of the key to press. Default "enter".

    Returns:
        Dict with keypress result.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.press(key)
        return {"status": "success", "detail": f"Pressed: {key}"}
    except ImportError:
        return {"status": "error", "detail": "PyAutoGUI not installed. Install with: pip install pyautogui"}
    except Exception as e:
        return {"status": "error", "detail": f"Keypress failed: {e}"}


def _screen_find(description: str) -> dict[str, Any]:
    """Find a UI element's coordinates on screen using Gemini vision.

    Takes a screenshot and asks Gemini to locate the described element.
    Returns the center coordinates (x, y) if found.

    Args:
        description: Natural language description of the element (e.g. "the search button", "the login input field").

    Returns:
        Dict with coordinates if found, or a NOT_FOUND status.
    """
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        w, h = pyautogui.size()

        screenshot = pyautogui.screenshot()
        import io
        buf = io.BytesIO()
        screenshot.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        from agent.vision import analyze_image_with_gemini

        analysis_prompt = (
            f"This is a screenshot of a {w}x{h} pixel screen. "
            f"Locate the UI element described as: '{description}'. "
            f"Reply with ONLY the center coordinates as: x,y "
            f"If the element is not visible, reply: NOT_FOUND"
        )

        import re
        text = _run_async(
            analyze_image_with_gemini(image_bytes, "image/png", prompt=analysis_prompt)
        )

        text = (text or "").strip()
        if "NOT_FOUND" in text.upper():
            return {"status": "not_found", "detail": f"Element '{description}' not found on screen"}

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            x, y = int(match.group(1)), int(match.group(2))
            return {
                "status": "success",
                "x": x,
                "y": y,
                "detail": f"Found '{description}' at ({x}, {y})",
            }
        return {"status": "error", "detail": f"Could not parse coordinates from Gemini response: {text[:100]}"}

    except ImportError as e:
        return {"status": "error", "detail": f"Screen find dependencies missing: {e}"}
    except Exception as e:
        return {"status": "error", "detail": f"Screen find failed: {e}"}


def _screen_click(description: str) -> dict[str, Any]:
    """Find a UI element by description and click it (screen_find + click).

    Args:
        description: Natural language description of the element to click.

    Returns:
        Dict with click result.
    """
    try:
        result = _screen_find(description)
        if result.get("status") == "success":
            x, y = result["x"], result["y"]
            import pyautogui
            pyautogui.click(x, y)
            return {"status": "success", "detail": f"Clicked '{description}' at ({x}, {y})"}
        elif result.get("status") == "not_found":
            return result
        return {"status": "error", "detail": result.get("detail", "Unknown error finding element")}
    except Exception as e:
        return {"status": "error", "detail": f"Screen click failed: {e}"}


# ─── Vision / Screen Analysis Functions (like Mark-L) ───────────────────

def _run_async(coro):
    """Run a coroutine synchronously from a thread pool context.

    Uses ``new_event_loop()`` since we're in a thread (called via
    ``asyncio.to_thread()`` from ``execute_function()``).
    Falls back to ``asyncio.run()`` if a loop was already set.
    """
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(coro)
        loop.close()
        return result
    except RuntimeError:
        return asyncio.run(coro)


def _analyze_screen(prompt: str = "What do you see on my screen? Be concise.") -> dict[str, Any]:
    """Capture the screen and analyze it using Gemini vision.

    Requires:
        - mss (screen capture)
        - google-genai (Gemini API)
        - GEMINI_API_KEY configured in .env or config/api_keys.json

    Args:
        prompt: The question or instruction about the screen content.

    Returns:
        Dict with analysis text and image metadata.
    """
    try:
        from agent.vision import capture_screen, analyze_image_with_gemini

        image_bytes, mime_type = capture_screen()
        text = _run_async(
            analyze_image_with_gemini(image_bytes, mime_type, prompt=prompt)
        )
        return {
            "status": "success",
            "analysis": text,
            "source": "screen",
            "image_size_bytes": len(image_bytes),
        }
    except ImportError as e:
        return {"status": "error", "detail": f"Vision dependencies not installed: {e}"}
    except Exception as e:
        return {"status": "error", "detail": f"Screen analysis failed: {e}"}

        return {
            "status": "success",
            "analysis": text,
            "source": "screen",
            "image_size_bytes": len(image_bytes),
        }
    except ImportError as e:
        return {"status": "error", "detail": f"Vision dependencies not installed: {e}"}
    except Exception as e:
        return {"status": "error", "detail": f"Screen analysis failed: {e}"}


def _analyze_camera(prompt: str = "What do you see? Be concise.") -> dict[str, Any]:
    """Capture the webcam and analyze it using Gemini vision.

    Requires:
        - opencv-python (webcam)
        - google-genai (Gemini API)
        - GEMINI_API_KEY configured in .env or config/api_keys.json

    Args:
        prompt: The question or instruction about what the camera sees.

    Returns:
        Dict with analysis text and image metadata.
    """
    try:
        from agent.vision import capture_camera, analyze_image_with_gemini

        image_bytes, mime_type = capture_camera()
        text = _run_async(
            analyze_image_with_gemini(image_bytes, mime_type, prompt=prompt)
        )
        return {
            "status": "success",
            "analysis": text,
            "source": "camera",
            "image_size_bytes": len(image_bytes),
        }
    except ImportError as e:
        return {"status": "error", "detail": f"Camera dependencies not installed: {e}"}
    except Exception as e:
        return {"status": "error", "detail": f"Camera analysis failed: {e}"}

        return {
            "status": "success",
            "analysis": text,
            "source": "camera",
            "image_size_bytes": len(image_bytes),
        }
    except ImportError as e:
        return {"status": "error", "detail": f"Camera dependencies not installed: {e}"}
    except Exception as e:
        return {"status": "error", "detail": f"Camera analysis failed: {e}"}


def _analyze_file(image_path: str, prompt: str = "What is in this image? Be concise.") -> dict[str, Any]:
    """Analyze a local image file using Gemini vision.

    Requires:
        - google-genai (Gemini API)
        - GEMINI_API_KEY configured in .env or config/api_keys.json

    Args:
        image_path: Path to the image file on disk.
        prompt: The question about the image.

    Returns:
        Dict with analysis text.
    """
    try:
        from pathlib import Path
        path = Path(image_path).expanduser().resolve()
        if not path.is_file():
            return {"status": "error", "detail": f"File not found: {path}"}

        import mimetypes
        mime_type, _ = mimetypes.guess_type(str(path))
        if not mime_type or not mime_type.startswith("image/"):
            mime_type = "image/jpeg"

        image_bytes = path.read_bytes()

        from agent.vision import analyze_image_with_gemini
        text = _run_async(
            analyze_image_with_gemini(image_bytes, mime_type, prompt=prompt)
        )

        return {"status": "success", "analysis": text, "source": "file", "file_path": str(path)}
    except Exception as e:
        return {"status": "error", "detail": f"File analysis failed: {e}"}


def _check_vision() -> dict[str, Any]:
    """Check if vision capabilities are available (mss, opencv, Gemini key)."""
    result = {"screen_capture": False, "webcam": False, "gemini_api": False}
    try:
        import mss  # noqa: F401
        result["screen_capture"] = True
    except ImportError:
        pass
    try:
        import cv2  # noqa: F401
        result["webcam"] = True
    except ImportError:
        pass
    try:
        from google import genai  # noqa: F401
        result["gemini_api"] = True
    except ImportError:
        pass
    return result


def _vision_stream_start() -> dict[str, Any]:
    """Start the persistent Gemini Live vision stream session.

    Maintains a persistent WebSocket to Gemini Live for zero-latency
    screen/camera analysis.  Once started, use ``analyze_screen`` or
    ``analyze_camera`` with the ``use_stream`` parameter.
    """
    try:
        from agent.vision import ensure_vision_stream
        ok = ensure_vision_stream()
        return {
            "status": "connected" if ok else "timeout",
            "detail": "Vision stream ready" if ok else "Vision stream timed out",
        }
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _vision_stream_stop() -> dict[str, Any]:
    """Stop the persistent Gemini Live vision stream session."""
    try:
        from agent.vision import stop_vision_stream
        stop_vision_stream()
        return {"status": "success", "detail": "Vision stream stopped"}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _vision_stream_status() -> dict[str, Any]:
    """Check the persistent vision stream session status."""
    try:
        from agent.vision import get_vision_stream_session
        session = get_vision_stream_session()
        if session:
            return {
                "status": "success",
                "connected": session.is_connected,
                "ready": session.is_ready,
            }
        return {"status": "success", "connected": False, "ready": False}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _vision_stream_analyze(
    prompt: str = "What do you see on my screen? Be concise.",
    source: str = "screen",
) -> dict[str, Any]:
    """Analyze the current screen or webcam through the persistent vision stream.

    Routes a fresh capture through the warm, long-lived Gemini Live vision
    stream for zero-latency analysis, returning the text description. Lets
    the voice agent see screen changes mid-conversation: ``vision_stream_start``
    once, then call this whenever the user asks what is on screen.

    Falls back to a direct Gemini REST analysis if the stream is not ready,
    so the tool always returns a description when vision is available.

    Args:
        prompt: The question or instruction about the screen content.
        source: "screen" (default) captures the monitor; "camera" captures the webcam.

    Returns:
        Dict with the analysis text and the path used ("stream" or "rest").
    """
    try:
        from agent.vision import (
            analyze_image_with_gemini,
            capture_camera,
            capture_screen,
            ensure_vision_stream,
            get_vision_stream_session,
        )

        source = (source or "screen").lower()
        if source not in ("screen", "camera"):
            source = "screen"

        # Best-effort: warm the persistent stream (short timeout so the
        # voice round-trip never blocks for the full 25s start window).
        stream_ok = ensure_vision_stream(timeout=12.0)

        image_bytes, mime_type = (
            capture_camera() if source == "camera" else capture_screen()
        )

        session = get_vision_stream_session() if stream_ok else None
        if stream_ok and session is not None and session.is_ready:
            try:
                text = session.analyze_and_wait(
                    image_bytes, mime_type, prompt, timeout=25.0
                )
                if text:
                    return {
                        "status": "success",
                        "analysis": text,
                        "source": source,
                        "via": "stream",
                        "image_size_bytes": len(image_bytes),
                    }
            except Exception as e:
                _safe_print(f"[VisionStream] Stream analysis failed ({e}) — falling back to REST")

        # Fallback: direct Gemini REST analysis (fresh connection).
        text = _run_async(
            analyze_image_with_gemini(image_bytes, mime_type, prompt=prompt)
        )
        return {
            "status": "success",
            "analysis": text,
            "source": source,
            "via": "rest",
            "image_size_bytes": len(image_bytes),
        }
    except ImportError as e:
        return {"status": "error", "detail": f"Vision dependencies not installed: {e}"}
    except Exception as e:
        return {"status": "error", "detail": f"Vision stream analysis failed: {e}"}


# ─── Function Registry ─────────────────────────────────────────────────

# ─── Browser Control Functions (Playwright) ────────────────────────────

def _browser_go_to(url: str, browser: str | None = None) -> dict[str, Any]:
    """Navigate to a URL in the browser."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("go_to", {"url": url, "browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_search(query: str, engine: str = "google", browser: str | None = None) -> dict[str, Any]:
    """Search the web."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("search", {"query": query, "engine": engine, "browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_click(selector: str | None = None, text: str | None = None, browser: str | None = None) -> dict[str, Any]:
    """Click an element on the current page."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("click", {"selector": selector, "text": text, "browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_type_text(text: str, selector: str | None = None, browser: str | None = None) -> dict[str, Any]:
    """Type text into an input field."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("type", {"text": text, "selector": selector, "browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_scroll(direction: str = "down", amount: int = 500, browser: str | None = None) -> dict[str, Any]:
    """Scroll the current page."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("scroll", {"direction": direction, "amount": amount, "browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_screenshot(browser: str | None = None) -> dict[str, Any]:
    """Take a screenshot of the current browser page."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("screenshot", {"browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_get_text(browser: str | None = None) -> dict[str, Any]:
    """Get visible text from the current page."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("get_text", {"browser": browser})
        return {"status": "success", "content": result, "detail": result[:200] + ('...' if len(result) > 200 else '')}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_new_tab(url: str = "", browser: str | None = None) -> dict[str, Any]:
    """Open a new browser tab."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("new_tab", {"url": url, "browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_close_tab(browser: str | None = None) -> dict[str, Any]:
    """Close the current browser tab."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("close_tab", {"browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_back(browser: str | None = None) -> dict[str, Any]:
    """Navigate back in browser history."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("back", {"browser": browser})
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_get_url(browser: str | None = None) -> dict[str, Any]:
    """Get the current browser page URL."""
    try:
        from system_control.browser_control import browser_action
        result = browser_action("get_url", {"browser": browser})
        return {"status": "success", "detail": result, "url": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _browser_open_native(url: str, browser: str | None = None) -> dict[str, Any]:
    """Open a URL in the user's native browser (no automation)."""
    try:
        from system_control.browser_control import open_url_native
        result = open_url_native(url, browser)
        return {"status": "success", "detail": result}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── Rich Content Broadcast Helper ─────────────────────────────────────

def _broadcast_rich_content(content: dict) -> None:
    """Fire-and-forget broadcast of structured data to the frontend Dynamic Content Panel.

    Tries to schedule the async ``broadcast_rich_content()`` call on the current
    running event loop via ``run_coroutine_threadsafe()``.  If no loop is running
    (e.g. called from a thread pool thread), falls back to ``_run_async()`` which
    creates a temporary event loop.

    Never raises — all errors are silently caught to avoid breaking the function
    result.
    """
    try:
        from voice.websocket_manager import VoiceWSManager
        mgr = VoiceWSManager.get_instance()
        try:
            loop = asyncio.get_running_loop()
            asyncio.run_coroutine_threadsafe(
                mgr.broadcast_rich_content(content),
                loop,
            )
        except RuntimeError:
            # No running loop (thread pool context) — use _run_async fallback
            _run_async(mgr.broadcast_rich_content(content))
    except Exception:
        pass  # Never let a broadcast break the function result


# ─── YouTube Control Functions ─────────────────────────────────────────

def _youtube_play(query: str) -> dict[str, Any]:
    """Search for a video on YouTube and play the first result."""
    try:
        from actions.youtube_control import youtube_play
        result = _run_async(youtube_play(query))
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _youtube_search(query: str, max_results: int = 5) -> dict[str, Any]:
    """Search YouTube videos by query."""
    try:
        from actions.youtube_control import youtube_search
        result = _run_async(youtube_search(query, max_results))
        # Broadcast rich content to frontend panel
        if result.get("status") == "ok" and result.get("results"):
            _broadcast_rich_content({
                "type": "youtube",
                "query": query,
                "results": result["results"],
                "summary": result.get("summary", ""),
            })
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _youtube_summarize(url: str, save: bool = False) -> dict[str, Any]:
    """Get transcript and summarize a YouTube video using AI."""
    try:
        from actions.youtube_control import youtube_summarize
        result = _run_async(youtube_summarize(url, save))
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _youtube_get_info(url: str) -> dict[str, Any]:
    """Get metadata for a YouTube video."""
    try:
        from actions.youtube_control import youtube_get_info
        result = _run_async(youtube_get_info(url))
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _youtube_trending(region: str = "US") -> dict[str, Any]:
    """Get trending YouTube videos."""
    try:
        from actions.youtube_control import youtube_trending
        result = _run_async(youtube_trending(region))
        # Broadcast rich content to frontend panel
        if result.get("status") == "ok" and result.get("results"):
            _broadcast_rich_content({
                "type": "youtube",
                "query": f"Trending in {region}",
                "results": result["results"],
                "summary": result.get("summary", ""),
            })
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── Flight Finder Functions ───────────────────────────────────────────

def _search_flights(
    origin: str = "",
    destination: str = "",
    date: str = "",
    return_date: str = "",
    passengers: int = 1,
    cabin: str = "economy",
) -> dict[str, Any]:
    """Search for flights using Google Flights."""
    if not origin or not destination:
        return {"status": "error", "detail": "Both origin and destination are required"}
    if not date:
        return {"status": "error", "detail": "Departure date is required"}
    try:
        from actions.flight_finder import search_flights
        result = _run_async(search_flights(
            origin=origin,
            destination=destination,
            date=date,
            return_date=return_date or None,
            passengers=max(1, passengers),
            cabin=cabin.lower(),
            open_browser=True,
        ))
        # Broadcast rich content to frontend panel
        if result.get("status") in ("ok", "partial") and result.get("results"):
            _broadcast_rich_content({
                "type": "flights",
                "origin": origin.upper(),
                "destination": destination.upper(),
                "date": date,
                "results": result["results"],
                "summary": result.get("summary", ""),
            })
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── Game Updater Functions ────────────────────────────────────────────

def _steam_list_games() -> dict[str, Any]:
    """List all installed Steam games with their update status."""
    try:
        from actions.game_updater import steam_list_games
        return steam_list_games()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _steam_update_game(game_name: str) -> dict[str, Any]:
    """Trigger update check for a specific Steam game."""
    if not game_name:
        return {"status": "error", "detail": "Game name is required"}
    try:
        from actions.game_updater import steam_update_game
        return steam_update_game(game_name)
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _steam_update_all() -> dict[str, Any]:
    """Trigger updates for all Steam games that need it."""
    try:
        from actions.game_updater import steam_update_all
        return steam_update_all()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _steam_install_game(game_name: str) -> dict[str, Any]:
    """Install a Steam game by name."""
    if not game_name:
        return {"status": "error", "detail": "Game name is required"}
    try:
        from actions.game_updater import steam_install_game
        return steam_install_game(game_name)
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _epic_list_games() -> dict[str, Any]:
    """List games installed via Epic Games Launcher."""
    try:
        from actions.game_updater import epic_list_games
        return epic_list_games()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _get_free_games() -> dict[str, Any]:
    """Get free-to-play games and current Steam deals."""
    try:
        from actions.game_updater import get_free_games
        result = _run_async(get_free_games())
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _check_game_updates() -> dict[str, Any]:
    """Check which installed games have pending updates."""
    try:
        from actions.game_updater import check_game_updates
        return check_game_updates()
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── File Processor Functions (Universal file processing) ──────────────

def _process_file(
    file_path: str = "",
    action: str = "",
    instruction: str = "",
    save: bool = True,
    **kwargs,
) -> dict[str, Any]:
    """Process any file type with AI-powered analysis and transformations.

    Automatically detects file type and dispatches to the right handler.
    Supports: images (describe/ocr/resize/convert/compress), PDFs (summarize/extract),
    documents (summarize/word_count), data files (analyze/filter/sort/convert),
    JSON (validate/format/analyze), code (explain/review/fix), audio (transcribe),
    video (info/compress/trim), archives (list/extract), presentations (summarize).

    Args:
        file_path: Path to the file (required).
        action: Action to perform (type-dependent).
        instruction: Custom instruction for AI analysis.
        save: Whether to save long results to disk (default: True).
        **kwargs: Extra params (width, height, quality, format, etc.).

    Returns:
        Dict with status and detail.
    """
    try:
        from actions.file_processor import process_file as _do_process
        result = _do_process(
            file_path=file_path,
            action=action,
            instruction=instruction,
            save=save,
            **kwargs,
        )
        return result
    except Exception as e:
        return {"status": "error", "detail": f"File processing failed: {e}"}


# ─── Messaging Functions (Desktop Automation) ──────────────────────────

def _send_message(
    platform: str = "whatsapp",
    receiver: str = "",
    message_text: str = "",
) -> dict[str, Any]:
    """Send a message via desktop automation (WhatsApp, Telegram, Discord, etc.).

    Opens the native messaging app, searches for the contact, pastes the
    message, and presses Enter.

    Args:
        platform: Platform name (whatsapp, telegram, signal, discord, messenger, instagram, slack).
        receiver: Contact name, phone, or username.
        message_text: Message content to send.

    Returns:
        Dict with send result.
    """
    try:
        from actions.send_message import send_message as _do_send
        result = _do_send(platform=platform, receiver=receiver, message_text=message_text)
        return result
    except Exception as e:
        return {"status": "error", "detail": f"Messaging failed: {e}"}


# ─── Reminder Functions ───────────────────────────────────────────────

def _set_reminder(title: str, message: str = "", delay_minutes: int = 5) -> dict[str, Any]:
    """Set a timed reminder with native OS toast notification."""
    try:
        from notifications.reminders import reminder_manager
        result = _run_async(reminder_manager.create_reminder(
            title=title,
            message=message,
            delay_minutes=max(1, delay_minutes),
        ))
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _list_reminders() -> dict[str, Any]:
    """List all active (non-dismissed) reminders."""
    try:
        from notifications.reminders import reminder_manager
        reminders = _run_async(reminder_manager.list_reminders())
        return {"status": "ok", "count": len(reminders), "reminders": reminders}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _dismiss_reminder(reminder_id: int) -> dict[str, Any]:
    """Dismiss a reminder by its ID."""
    try:
        from notifications.reminders import reminder_manager
        result = _run_async(reminder_manager.dismiss_reminder(reminder_id))
        return result
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── Code Helper & Dev Agent Functions (async → sync wrappers) ────────

def _code_helper(
    action: str = "auto",
    description: str = "",
    language: str = "python",
    file_path: str = "",
    output_path: str = "",
    code: str = "",
) -> dict[str, Any]:
    """Generate, edit, explain, run, build, or debug code using an LLM.

    Args:
        action: "write" | "edit" | "explain" | "run" | "build" | "optimize" | "screen_debug" | "auto"
        description: What the code should do / what change to make
        language: Programming language (default: python)
        file_path: Path to existing file
        output_path: Where to save the output
        code: Raw code string for explain/optimize

    Returns:
        Dict with human-readable result.
    """
    try:
        from actions.code_helper import code_helper
        result = _run_async(code_helper({
            "action": action,
            "description": description,
            "language": language,
            "file_path": file_path,
            "output_path": output_path,
            "code": code,
        }))
        truncated = len(result) > 5000
        return {"status": "success", "detail": result[:5000], "truncated": truncated}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


def _dev_agent(
    description: str = "",
    language: str = "python",
    project_name: str = "",
) -> dict[str, Any]:
    """Build a complete software project from a natural language description.

    Plans the structure, writes all files, installs deps, runs, and auto-fixes.

    Args:
        description: What project to build (required)
        language: Programming language (default: python)
        project_name: Optional project directory name

    Returns:
        Dict with build report.
    """
    if not description:
        return {"status": "error", "detail": "Please describe the project you want me to build."}
    try:
        from actions.dev_agent import dev_agent
        result = _run_async(dev_agent({
            "description": description,
            "language": language,
            "project_name": project_name,
        }))
        truncated = len(result) > 5000
        return {"status": "success", "detail": result[:5000], "truncated": truncated}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


# ─── Function Registry ─────────────────────────────────────────────────

FUNCTION_REGISTRY: dict[str, Any] = {
    "minimize_window": _minimize_window,
    "maximize_window": _maximize_window,
    "open_file": _open_file,
    "launch_app": _launch_app,
    "close_app": _close_app,
    "get_system_status": _get_system_status,
    "get_hardware_status": _get_hardware_status,
    "start_hardware_monitoring": _start_hardware_monitoring,
    "stop_hardware_monitoring": _stop_hardware_monitoring,
    "get_hardware_alerts": _get_hardware_alerts,
    "list_files": _list_files,
    "run_shell_command": _run_shell_command,
    "take_screenshot": _take_screenshot,
    "clipboard": _clipboard_op,
    "focus_window": _focus_window,
    "set_app_volume": _set_app_volume,
    "mute_volume": _mute_volume,
    "media_control": _media_control,
    "empty_trash": _empty_trash,
    "lock_screen": _lock_screen,
    # Mouse & Keyboard control (PyAutoGUI)
    "mouse_click": _mouse_click,
    "mouse_move": _mouse_move,
    "mouse_drag": _mouse_drag,
    "mouse_scroll": _mouse_scroll,
    "keyboard_type": _keyboard_type,
    "keyboard_smart_type": _keyboard_smart_type,
    "keyboard_hotkey": _keyboard_hotkey,
    "keyboard_press": _keyboard_press,
    "screen_find": _screen_find,
    "screen_click": _screen_click,
    "analyze_screen": _analyze_screen,
    "analyze_camera": _analyze_camera,
    "analyze_file": _analyze_file,
    "check_vision": _check_vision,
    "vision_stream_start": _vision_stream_start,
    "vision_stream_stop": _vision_stream_stop,
    "vision_stream_status": _vision_stream_status,
    "vision_stream_analyze": _vision_stream_analyze,
    # Browser control
    "browser_go_to": _browser_go_to,
    "browser_search": _browser_search,
    "browser_click": _browser_click,
    "browser_type_text": _browser_type_text,
    "browser_scroll": _browser_scroll,
    "browser_screenshot": _browser_screenshot,
    "browser_get_text": _browser_get_text,
    "browser_get_url": _browser_get_url,
    "browser_new_tab": _browser_new_tab,
    "browser_close_tab": _browser_close_tab,
    "browser_back": _browser_back,
    "browser_open_native": _browser_open_native,
    # Code helper & Dev agent
    "code_helper": _code_helper,
    "dev_agent": _dev_agent,
    # YouTube
    "youtube_play": _youtube_play,
    "youtube_search": _youtube_search,
    "youtube_summarize": _youtube_summarize,
    "youtube_get_info": _youtube_get_info,
    "youtube_trending": _youtube_trending,
    # Flight finder
    "search_flights": _search_flights,
    # Game updater
    "steam_list_games": _steam_list_games,
    "steam_update_game": _steam_update_game,
    "steam_update_all": _steam_update_all,
    "steam_install_game": _steam_install_game,
    "epic_list_games": _epic_list_games,
    "get_free_games": _get_free_games,
    "check_game_updates": _check_game_updates,
    # Reminders
    "set_reminder": _set_reminder,
    "list_reminders": _list_reminders,
    "dismiss_reminder": _dismiss_reminder,
    # Messaging (Desktop automation: WhatsApp, Telegram, Discord, etc.)
    # File Processor (AI-powered file analysis & transformation)
    "process_file": _process_file,
    "send_message": _send_message,
}

# ─── BARQ Feature Bridge ────────────────────────────────────────────────
# Voice access to every BARQ feature (jobs, social, memory, brain, workflows,
# notifications, analytics, settings, agent skills, ...) via self-HTTP dispatch
# and the SkillRegistry. See voice/feature_bridge.py for details.
from .feature_bridge import (  # noqa: E402
    FEATURE_FUNCTIONS as BARQ_FEATURE_FUNCTIONS,
    FEATURE_SCHEMAS as BARQ_FEATURE_SCHEMAS,
)

FUNCTION_REGISTRY.update(BARQ_FEATURE_FUNCTIONS)


def get_function_schemas() -> list[dict]:
    """Return the function schemas to inject into think.functions.

    These are the tool definitions sent in the Settings payload so Deepgram's
    Gemini Flash knows which local functions are available.
    """
    return [
        {
            "name": "minimize_window",
            "description": "Minimizes the currently active window or a target application window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_name": {
                        "type": "string",
                        "description": "Optional name of the specific window to minimize (e.g. 'Chrome', 'VS Code'). If omitted, minimizes active.",
                    },
                },
            },
        },
        {
            "name": "maximize_window",
            "description": "Maximizes the currently active window or a target application window.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_name": {
                        "type": "string",
                        "description": "Optional name of the specific window to maximize.",
                    },
                },
            },
        },
        {
            "name": "open_file",
            "description": "Opens a local file or application using the default OS handler.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "The exact absolute path or relative file name to open.",
                    },
                },
                "required": ["file_path"],
            },
        },
        {
            "name": "launch_app",
            "description": "Launches an application by name or path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name_or_path": {
                        "type": "string",
                        "description": "Name of the app to launch (e.g. 'notepad', 'code', '/usr/bin/firefox').",
                    },
                },
                "required": ["app_name_or_path"],
            },
        },
        {
            "name": "close_app",
            "description": "Closes an application by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Name of the application process to close (e.g. 'chrome', 'notepad').",
                    },
                },
                "required": ["app_name"],
            },
        },
        {
            "name": "get_system_status",
            "description": "Returns current system status including CPU, memory, and disk usage.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "get_hardware_status",
            "description": "Returns comprehensive hardware status including CPU, RAM, GPU, disk, network speed, and system uptime. Use this when the user asks about hardware health, system performance, GPU temperature, or any detailed hardware question.",
            "parameters": {
                "type": "object",
                "properties": {
                    "detailed": {
                        "type": "boolean",
                        "description": "If true, returns full telemetry with top processes and GPU details. Default: false.",
                    },
                },
            },
        },
        {
            "name": "start_hardware_monitoring",
            "description": "Starts background hardware monitoring with configurable threshold alerts. Will automatically fire desktop notifications when CPU, RAM, disk, or GPU exceed limits. Call this when the user says 'monitor my hardware', 'watch my system', or 'alert me about high usage'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "interval": {
                        "type": "number",
                        "description": "Seconds between telemetry checks. Default: 5.0. Range: 1-60.",
                    },
                },
            },
        },
        {
            "name": "stop_hardware_monitoring",
            "description": "Stops background hardware monitoring and threshold alerts.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "get_hardware_alerts",
            "description": "Returns any active hardware threshold alerts (high CPU, RAM, GPU temperature, etc.). Use this when the user asks 'are there any alerts' or 'is my system healthy'.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "list_files",
            "description": "Lists files in a directory with optional glob pattern.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Directory path to list files from.",
                    },
                    "pattern": {
                        "type": "string",
                        "description": "Optional glob pattern (e.g. '*.txt', '**/*.py'). Defaults to '*'.",
                    },
                },
                "required": ["directory"],
            },
        },
        {
            "name": "run_shell_command",
            "description": "Runs a shell command and returns its output. For quick terminal tasks like checking disk space, listing processes, or git status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to execute. Must be a single command (no pipes/shell features).",
                    },
                },
                "required": ["command"],
            },
        },
        {
            "name": "take_screenshot",
            "description": "Takes a screenshot of the entire screen and saves it to a file. Optionally specify a file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Optional path to save the screenshot. If omitted, saves to a temporary file with a timestamp.",
                    },
                },
            },
        },
        {
            "name": "clipboard",
            "description": "Reads from or writes to the system clipboard. Use action='read' to get clipboard contents, action='write' with 'text' to set clipboard contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "write"],
                        "description": "'read' to get clipboard contents, 'write' to set clipboard contents.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Text to write to clipboard (required when action='write').",
                    },
                },
            },
        },
        {
            "name": "focus_window",
            "description": "Brings a specific application window to the foreground by name or title substring.",
            "parameters": {
                "type": "object",
                "properties": {
                    "window_name": {
                        "type": "string",
                        "description": "Name or title substring of the window to bring to front (e.g. 'Chrome', 'VS Code', 'Notepad').",
                    },
                },
                "required": ["window_name"],
            },
        },
        {
            "name": "set_app_volume",
            "description": "Sets the volume level for a specific application or the system master volume. Level ranges from 0 (mute) to 100 (max).",
            "parameters": {
                "type": "object",
                "properties": {
                    "app_name": {
                        "type": "string",
                        "description": "Optional name of the application (e.g. 'chrome.exe', 'spotify.exe'). If empty, sets the system master volume.",
                    },
                    "level": {
                        "type": "integer",
                        "description": "Volume level from 0 (mute) to 100 (max). Default 50.",
                    },
                },
            },
        },
        {
            "name": "mute_volume",
            "description": "Mutes or unmutes the system volume. Set mute=true to mute, mute=false to unmute.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mute": {
                        "type": "boolean",
                        "description": "True to mute, False to unmute. Default is True.",
                    },
                },
            },
        },
        {
            "name": "media_control",
            "description": "Controls media playback: play/pause, next track, or previous track.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["play_pause", "next", "previous"],
                        "description": "Action to perform: 'play_pause', 'next', or 'previous'.",
                    },
                },
            },
        },
        {
            "name": "empty_trash",
            "description": "Empties the system trash/recycle bin.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "lock_screen",
            "description": "Locks the computer screen immediately.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        # ── Mouse & Keyboard Control (PyAutoGUI) ───────────────────────────
        {
            "name": "mouse_click",
            "description": "Clicks at specified screen coordinates or current cursor position. Supports left, right, and middle buttons. Use double_click by setting clicks=2.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Optional X coordinate. If omitted, clicks at current cursor position."},
                    "y": {"type": "integer", "description": "Optional Y coordinate. If omitted, clicks at current cursor position."},
                    "button": {"type": "string", "enum": ["left", "right", "middle"], "description": "Mouse button. Default: left."},
                    "clicks": {"type": "integer", "description": "Number of clicks. 1=single, 2=double. Default: 1."},
                },
            },
        },
        {
            "name": "mouse_move",
            "description": "Moves the mouse cursor to absolute screen coordinates.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "integer", "description": "Target X coordinate (required)."},
                    "y": {"type": "integer", "description": "Target Y coordinate (required)."},
                    "duration": {"type": "number", "description": "Animation duration in seconds. Default: 0.3."},
                },
                "required": ["x", "y"],
            },
        },
        {
            "name": "mouse_drag",
            "description": "Click-drags from one point to another. Useful for selecting text, moving windows, or drawing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "x1": {"type": "integer", "description": "Starting X coordinate (required)."},
                    "y1": {"type": "integer", "description": "Starting Y coordinate (required)."},
                    "x2": {"type": "integer", "description": "Ending X coordinate (required)."},
                    "y2": {"type": "integer", "description": "Ending Y coordinate (required)."},
                    "duration": {"type": "number", "description": "Drag duration in seconds. Default: 0.5."},
                },
                "required": ["x1", "y1", "x2", "y2"],
            },
        },
        {
            "name": "mouse_scroll",
            "description": "Scrolls the mouse wheel in any direction. Use for scrolling web pages, documents, or lists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "Scroll direction. Default: down."},
                    "amount": {"type": "integer", "description": "Number of scroll clicks. Default: 3."},
                },
            },
        },
        {
            "name": "keyboard_type",
            "description": "Types text at the current cursor position, character by character.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type (required)."},
                    "interval": {"type": "number", "description": "Seconds between keystrokes. Default: 0.03."},
                },
                "required": ["text"],
            },
        },
        {
            "name": "keyboard_smart_type",
            "description": "Types text using clipboard paste for longer text (>20 chars). Faster and more reliable than character-by-character typing. Can clear the field first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to type (required)."},
                    "clear_first": {"type": "boolean", "description": "Select-all and delete first. Default: true."},
                },
                "required": ["text"],
            },
        },
        {
            "name": "keyboard_hotkey",
            "description": "Presses a keyboard shortcut / combination (e.g. ctrl+c, alt+tab, ctrl+shift+esc).",
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of key names to press together, e.g. ['ctrl', 'c'] for copy, ['alt', 'tab'] for window switch.",
                    },
                },
                "required": ["keys"],
            },
        },
        {
            "name": "keyboard_press",
            "description": "Presses a single keyboard key (e.g. enter, tab, escape, space, backspace, arrow keys).",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Name of the key to press (e.g. 'enter', 'tab', 'escape', 'space'). Default: enter."},
                },
            },
        },
        {
            "name": "screen_find",
            "description": "Finds a UI element on screen by describing it in natural language. Uses AI vision to locate buttons, text fields, icons, or any visible element. Returns the center coordinates (x, y).",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Natural language description of the element to find, e.g. 'the search button', 'the login input field', 'the submit button with text Send'."},
                },
                "required": ["description"],
            },
        },
        {
            "name": "screen_click",
            "description": "Finds a UI element by description and clicks it. Combines screen_find + click in one step.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Natural language description of the element to click."},
                },
                "required": ["description"],
            },
        },
        # ── Browser Control Functions (Playwright) ────────────────────────
        {
            "name": "browser_go_to",
            "description": "Opens a website or navigates to a URL in the user's web browser. Call this when the user says 'go to', 'open', 'navigate to', or 'browse' a website. Supports Chrome, Edge, Brave, and Firefox.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL or website name to navigate to (e.g. 'github.com', 'youtube.com', 'gmail.com').",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use ('chrome', 'edge', 'brave', 'firefox'). Defaults to Chrome.",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "browser_search",
            "description": "Searches the web using a search engine (Google by default). Call this when the user says 'search for', 'look up', 'google', or 'find' something online.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query (e.g. 'Python jobs in New York', 'best restaurants near me').",
                    },
                    "engine": {
                        "type": "string",
                        "enum": ["google", "bing", "duckduckgo"],
                        "description": "Search engine to use. Default: google.",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "browser_click",
            "description": "Clicks an element on the current browser page by its visible text or CSS selector. Call this when the user says 'click', 'press', or 'select' something on a webpage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The visible text of the element to click (e.g. 'Sign in', 'Submit', 'Login').",
                    },
                    "selector": {
                        "type": "string",
                        "description": "Optional: CSS selector as alternative to text.",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
            },
        },
        {
            "name": "browser_type_text",
            "description": "Types text into an input field on the current browser page. Call this when the user says 'type', 'enter', 'fill in', or 'write' something in a form.",
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "The text to type into the input field.",
                    },
                    "selector": {
                        "type": "string",
                        "description": "Optional: CSS selector for the input field. If omitted, types into the focused element.",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
                "required": ["text"],
            },
        },
        {
            "name": "browser_scroll",
            "description": "Scrolls the current browser page up or down. Call this when the user says 'scroll down', 'scroll up', or 'page down'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["down", "up"],
                        "description": "Scroll direction: 'down' or 'up'. Default: down.",
                    },
                    "amount": {
                        "type": "integer",
                        "description": "Number of pixels to scroll. Default: 500.",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
            },
        },
        {
            "name": "browser_get_text",
            "description": "Gets the visible text content from the current browser page. Call this when the user asks 'what does the page say', 'read the page', or 'get text from the page'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
            },
        },
        {
            "name": "browser_get_url",
            "description": "Gets the current URL of the browser page. Call this when the user asks 'what's the current URL' or 'what page am I on'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
            },
        },
        {
            "name": "browser_new_tab",
            "description": "Opens a new browser tab, optionally navigating to a URL. Call this when the user says 'new tab' or 'open in new tab'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Optional: URL to open in the new tab.",
                    },
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
            },
        },
        {
            "name": "browser_close_tab",
            "description": "Closes the current browser tab. Call this when the user says 'close tab' or 'close this tab'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
            },
        },
        {
            "name": "browser_back",
            "description": "Navigates back in the browser history. Call this when the user says 'go back' or 'back'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "browser": {
                        "type": "string",
                        "description": "Optional: browser to use.",
                    },
                },
            },
        },
        # ── Code Helper & Dev Agent ─────────────────────────────────────
        {
            "name": "code_helper",
            "description": "Generates, edits, explains, runs, builds, or debugs code using an LLM. Call this when the user wants you to write code, fix a bug, explain code, build a program, or debug an error. Supports multiple programming languages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["auto", "write", "edit", "explain", "run", "build", "optimize", "screen_debug"],
                        "description": "Action to perform: 'auto' (auto-detect), 'write', 'edit', 'explain', 'run', 'build', 'optimize', or 'screen_debug'. Default: auto.",
                    },
                    "description": {
                        "type": "string",
                        "description": "What the code should do or what change to make. Required for write/edit/build actions.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Programming language: python, javascript, typescript, html, css, java, cpp, bash, rust, go, sql, etc. Default: python.",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Path to existing file for edit/explain/run actions.",
                    },
                },
            },
        },
        {
            "name": "dev_agent",
            "description": "Builds a complete software project from a natural language description. Plans the file structure, writes all files in dependency order, installs dependencies, runs the project, and auto-fixes errors. Call this when the user says 'build a project', 'create an app', or 'make a program'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What project to build (e.g. 'a Flask REST API for a todo app', 'a CLI tool for file management'). Required.",
                    },
                    "language": {
                        "type": "string",
                        "description": "Programming language: python, javascript, typescript, etc. Default: python.",
                    },
                    "project_name": {
                        "type": "string",
                        "description": "Optional custom project directory name.",
                    },
                },
                "required": ["description"],
            },
        },
        # ── Vision Functions (like Mark-L) ────────────────────────────────
        {
            "name": "analyze_screen",
            "description": "Captures your computer screen and analyzes it using AI vision. Call this when the user asks what is on their screen, what you can see, or any question about the current display content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The question or instruction about what's on screen. Default: 'What do you see on my screen? Be concise.'",
                    },
                },
            },
        },
        {
            "name": "analyze_camera",
            "description": "Captures the webcam and analyzes it using AI vision. Call this when the user asks you to look at them, see what's in front of the camera, or take a photo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "The question about what the camera sees. Default: 'What do you see? Be concise.'",
                    },
                },
            },
        },
        {
            "name": "analyze_file",
            "description": "Analyzes a local image file using AI vision. Provide the file path and an optional question about the image.",
            "parameters": {
                "type": "object",
                "properties": {
                    "image_path": {
                        "type": "string",
                        "description": "Full path to the image file on disk.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Optional question about the image. Default: 'What is in this image? Be concise.'",
                    },
                },
                "required": ["image_path"],
            },
        },
        {
            "name": "check_vision",
            "description": "Checks whether vision capabilities are available (screen capture library, webcam library, Gemini API key). Returns which components are ready.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "vision_stream_start",
            "description": "Starts BARQ's persistent Gemini Live vision stream — a warm, long-lived connection for zero-latency screen/webcam analysis. Call once at the start of a conversation where the user may ask about the screen, then use vision_stream_analyze to see screen changes instantly.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "vision_stream_stop",
            "description": "Stops the persistent Gemini Live vision stream and releases its connection.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "vision_stream_status",
            "description": "Checks whether the persistent Gemini Live vision stream is connected and ready.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "vision_stream_analyze",
            "description": "Analyzes the CURRENT screen (or webcam) through the warm persistent vision stream and returns a text description of what is on screen right now. Call when the user asks what is on their screen, what changed on screen, what you can see, or any question about live display content. Falls back to a direct analysis if the stream is not warm.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Question or instruction about the screen content. Default: 'What do you see on my screen? Be concise.'",
                    },
                    "source": {
                        "type": "string",
                        "description": "'screen' (default) captures the monitor; 'camera' captures the webcam.",
                    },
                },
            },
        },
        # ── YouTube Functions ────────────────────────────────────────────
        {
            "name": "youtube_play",
            "description": "Searches YouTube for a video and opens the first result in your browser. Call this when you say 'play' or 'watch' a video on YouTube.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query for what to play (e.g. 'never gonna give you up', 'python tutorial').",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "youtube_search",
            "description": "Searches YouTube and returns a list of matching video results with titles, channels, and durations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query (e.g. 'tech reviews', 'music').",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results to return (1-10). Default: 5.",
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "youtube_summarize",
            "description": "Fetches the transcript of a YouTube video and summarizes it using AI. Call this when you want to understand a long video without watching it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full YouTube video URL.",
                    },
                    "save": {
                        "type": "boolean",
                        "description": "If true, saves the summary to Desktop as a text file.",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "youtube_get_info",
            "description": "Gets metadata for a YouTube video including title, channel, views, duration, and likes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The full YouTube video URL.",
                    },
                },
                "required": ["url"],
            },
        },
        {
            "name": "youtube_trending",
            "description": "Gets the current trending YouTube videos for a region.",
            "parameters": {
                "type": "object",
                "properties": {
                    "region": {
                        "type": "string",
                        "description": "ISO 3166-1 alpha-2 country code (e.g. 'US', 'IN', 'GB'). Default: 'US'.",
                    },
                },
            },
        },
        # ── Flight Finder ────────────────────────────────────────────────
        {
            "name": "search_flights",
            "description": "Searches for flights using Google Flights. Opens the results in your browser and returns flight options with prices, airlines, and durations. Call this when you want to find flights for travel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "origin": {
                        "type": "string",
                        "description": "Departure airport or city code (e.g. 'JFK', 'New York', 'DEL').",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Arrival airport or city code (e.g. 'LHR', 'London', 'DXB').",
                    },
                    "date": {
                        "type": "string",
                        "description": "Departure date. Supports natural language like 'tomorrow', 'next Monday', or YYYY-MM-DD format.",
                    },
                    "return_date": {
                        "type": "string",
                        "description": "Optional return date for round trips.",
                    },
                    "passengers": {
                        "type": "integer",
                        "description": "Number of passengers. Default: 1.",
                    },
                    "cabin": {
                        "type": "string",
                        "enum": ["economy", "premium", "business", "first"],
                        "description": "Cabin class. Default: economy.",
                    },
                },
                "required": ["origin", "destination", "date"],
            },
        },
        # ── Game Updater ─────────────────────────────────────────────────
        {
            "name": "steam_list_games",
            "description": "Lists all installed Steam games with their update status (up to date, downloading, update pending).",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "steam_update_game",
            "description": "Triggers an update check for a specific Steam game. Call this when you want to update a particular game.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_name": {
                        "type": "string",
                        "description": "Name of the game to update (e.g. 'Counter-Strike 2', 'Dota 2', 'Cyberpunk 2077').",
                    },
                },
                "required": ["game_name"],
            },
        },
        {
            "name": "steam_update_all",
            "description": "Triggers update checks for all Steam games that have pending updates. Call this to update all your games at once.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "steam_install_game",
            "description": "Installs a Steam game by searching for its AppID and opening the Steam install dialog.",
            "parameters": {
                "type": "object",
                "properties": {
                    "game_name": {
                        "type": "string",
                        "description": "Name of the game to install (e.g. 'Elden Ring', 'Rust', 'Valheim').",
                    },
                },
                "required": ["game_name"],
            },
        },
        {
            "name": "epic_list_games",
            "description": "Lists games installed via the Epic Games Launcher.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "get_free_games",
            "description": "Finds free-to-play games and current Steam deals/discounts.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
        # ── Messaging (Desktop Automation) ─────────────────────────────────
        {
            "name": "send_message",
            "description": "Sends a message to a contact via a desktop messaging app. Opens the app, searches for the recipient, and sends the text. Supports WhatsApp, Telegram, Signal, Discord, Messenger, Instagram, and Slack.",
            "parameters": {
                "type": "object",
                "properties": {
                    "platform": {
                        "type": "string",
                        "enum": ["whatsapp", "telegram", "signal", "discord", "messenger", "instagram", "slack"],
                        "description": "Messaging platform to use. Supports: whatsapp, telegram, signal, discord, messenger, instagram, slack. Default: whatsapp.",
                    },
                    "receiver": {
                        "type": "string",
                        "description": "Contact name, phone number, or username to send the message to (required).",
                    },
                    "message_text": {
                        "type": "string",
                        "description": "The text content of the message to send (required).",
                    },
                },
                "required": ["receiver", "message_text"],
            },
        },
        # ── File Processor (AI-powered file analysis) ──────────────────────
        {
            "name": "process_file",
            "description": "Processes any file type with AI-powered analysis and transformations. Automatically detects file type. Supports: images (describe, ocr, resize, convert, compress), PDFs (summarize, extract_text, to_word), documents (summarize, word_count, reformat), data files (analyze, filter, sort, convert), JSON (validate, format, analyze), code (explain, review, fix, optimize, document, run), audio (transcribe, info, convert, trim), video (info, extract_audio, trim, compress, transcribe), archives (list, extract), presentations (summarize, extract_text). Requires optional libraries: Pillow, pdfplumber, python-docx, pandas, openpyxl, pydub, python-pptx, ffmpeg.",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Absolute or relative path to the file (required).",
                    },
                    "action": {
                        "type": "string",
                        "description": "Action to perform. Type-dependent: image(describe, ocr, resize, convert, compress, info), pdf(summarize, extract_text, info, to_word), docx(summarize, extract_text, word_count), csv/excel(analyze, info, stats, filter, sort, convert), json(validate, format, analyze, to_csv), code(explain, review, fix, optimize, document, run, info), audio(transcribe, info, convert, trim), video(info, extract_audio, trim, extract_frame, compress, transcribe, convert), archive(list, extract), pptx(summarize, extract_text, analyze).",
                    },
                    "instruction": {
                        "type": "string",
                        "description": "Custom instruction override for AI analysis. Use this to ask specific questions about the file content.",
                    },
                    "save": {
                        "type": "boolean",
                        "description": "Whether to save long AI responses to disk (.txt file). Default: true.",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Target width for image resize action.",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Target height for image resize action.",
                    },
                    "scale": {
                        "type": "number",
                        "description": "Scale factor for image resize (e.g. 0.5 for half size).",
                    },
                    "quality": {
                        "type": "integer",
                        "description": "Compression quality 1-100. Default: 70 for images, 28 (CRF) for video.",
                    },
                    "format": {
                        "type": "string",
                        "description": "Target format for convert action. Image: png, jpg, webp. Audio: mp3, wav. Video: mp4, avi, mov.",
                    },
                    "column": {
                        "type": "string",
                        "description": "Column name for data file filter/sort operations.",
                    },
                    "value": {
                        "type": "string",
                        "description": "Value to filter by.",
                    },
                    "condition": {
                        "type": "string",
                        "enum": ["equals", "contains", "gt", "lt"],
                        "description": "Filter condition: equals, contains, gt (greater than), lt (less than).",
                    },
                    "start": {
                        "type": "string",
                        "description": "Start time for trim operations (e.g. '00:00:05' or seconds as number).",
                    },
                    "end": {
                        "type": "string",
                        "description": "End time for trim operations.",
                    },
                    "timestamp": {
                        "type": "string",
                        "description": "Timestamp for video frame extraction (e.g. '00:00:01').",
                    },
                    "destination": {
                        "type": "string",
                        "description": "Extraction destination directory path for archives.",
                    },
                },
                "required": ["file_path"],
            },
        },
    ] + BARQ_FEATURE_SCHEMAS


async def execute_function(function_name: str, arguments: dict) -> dict[str, Any]:
    """Execute a function by name and return its result.

    All synchronous OS calls are wrapped in asyncio.to_thread() to
    prevent blocking the voice audio stream.

    Args:
        function_name: The name of the function to execute.
        arguments: Dict of keyword arguments to pass to the function.

    Returns:
        Dict with function execution result (always has 'status' key).
    """
    func = FUNCTION_REGISTRY.get(function_name)
    if not func:
        return {"status": "error", "detail": f"Unknown function: {function_name}"}

    try:
        # Run the blocking OS function in a thread pool
        result = await asyncio.to_thread(func, **arguments)
        return result
    except TypeError as e:
        return {"status": "error", "detail": f"Invalid arguments for {function_name}: {e}"}
    except Exception as e:
        return {"status": "error", "detail": f"Execution error: {e}"}
