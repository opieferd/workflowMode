#!/usr/bin/env python3
"""workflowMode for Windows 10/11 laptop displays.

Built-in display brightness uses Windows WMI/CIM. External monitors and keyboard
backlights have no universal Windows API and aren't controlled by this script.
"""
import argparse
import ctypes
from ctypes import wintypes
import signal
import subprocess
import sys
import time

from pycaw.pycaw import AudioUtilities

DEFAULT_TIMEOUT = 3600
DEFAULT_BRIGHTNESS = 0
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
WH_KEYBOARD_LL = 13
WH_MOUSE_LL = 14
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
WM_QUIT = 0x0012
VK_C = 0x43
CTRL_KEYS = (0x11, 0xA2, 0xA3)
PM_REMOVE = 0x0001

running = True
old_brightness = None
old_mute = None
volume_endpoint = None
brightness_changed = False
audio_changed = False
awake = False
keyboard_hook = None
mouse_hook = None
keyboard_callback = None
mouse_callback = None
ctrl_down = set()

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(
    LRESULT, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
)


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


user32.SetWindowsHookExW.argtypes = (ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HANDLE
user32.CallNextHookEx.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = (wintypes.HANDLE,)
user32.UnhookWindowsHookEx.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = (
    ctypes.POINTER(wintypes.MSG), wintypes.HWND,
    wintypes.UINT, wintypes.UINT, wintypes.UINT
)
user32.PeekMessageW.restype = wintypes.BOOL
kernel32.SetThreadExecutionState.argtypes = (wintypes.DWORD,)
kernel32.SetThreadExecutionState.restype = wintypes.DWORD
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE


def ps(expression):
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", expression],
        capture_output=True, text=True, timeout=20, check=True
    )
    return result.stdout.strip()


def get_brightness():
    expression = (
        "$ErrorActionPreference='Stop';"
        "$m=Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness "
        "| Select-Object -First 1;"
        "if($null -eq $m){throw 'No supported built-in laptop brightness device'};"
        "[int]$m.CurrentBrightness"
    )
    value = int(ps(expression).splitlines()[-1])
    if not 0 <= value <= 100:
        raise RuntimeError(f"Unexpected brightness: {value}")
    return value


def set_brightness(value):
    value = int(value)
    if not 0 <= value <= 100:
        raise ValueError("Brightness must be 0..100.")
    expression = (
        "$ErrorActionPreference='Stop';"
        "$m=Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods "
        "| Select-Object -First 1;"
        "if($null -eq $m){throw 'No supported built-in display brightness control'};"
        f"Invoke-CimMethod -InputObject $m -MethodName WmiSetBrightness "
        f"-Arguments @{{Timeout=0;Brightness=[byte]{value}}} | Out-Null"
    )
    ps(expression)


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def next_hook(handle, code, wparam, lparam):
    return user32.CallNextHookEx(handle, code, wparam, lparam)


def key_filter(code, wparam, lparam):
    global running
    if code < 0:
        return next_hook(keyboard_hook, code, wparam, lparam)
    try:
        keycode = ctypes.cast(
            lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)
        ).contents.vkCode
        if keycode in CTRL_KEYS:
            if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                ctrl_down.add(keycode)
            elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                ctrl_down.discard(keycode)
        if keycode == VK_C and ctrl_down and wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
            running = False
        # Suppress ordinary keyboard events while the hook is active.
        return 1
    except Exception:
        return next_hook(keyboard_hook, code, wparam, lparam)


def mouse_filter(code, wparam, lparam):
    if code < 0:
        return next_hook(mouse_hook, code, wparam, lparam)
    return 1


def install_hooks():
    global keyboard_hook, mouse_hook, keyboard_callback, mouse_callback
    keyboard_callback = HOOKPROC(key_filter)
    mouse_callback = HOOKPROC(mouse_filter)
    module = kernel32.GetModuleHandleW(None)
    keyboard_hook = user32.SetWindowsHookExW(WH_KEYBOARD_LL, keyboard_callback, module, 0)
    if not keyboard_hook:
        raise ctypes.WinError(ctypes.get_last_error())
    mouse_hook = user32.SetWindowsHookExW(WH_MOUSE_LL, mouse_callback, module, 0)
    if not mouse_hook:
        remove_hooks()
        raise ctypes.WinError(ctypes.get_last_error())


def remove_hooks():
    global keyboard_hook, mouse_hook
    if mouse_hook:
        user32.UnhookWindowsHookEx(mouse_hook)
        mouse_hook = None
    if keyboard_hook:
        user32.UnhookWindowsHookEx(keyboard_hook)
        keyboard_hook = None
    ctrl_down.clear()


def pump_messages():
    msg = wintypes.MSG()
    while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
        if msg.message == WM_QUIT:
            stop()
            return
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def cleanup():
    global awake
    remove_hooks()
    if brightness_changed and old_brightness is not None:
        try:
            set_brightness(old_brightness)
            print("Display brightness restored.")
        except Exception as exc:
            print(f"Display restore failed: {exc}")
    if audio_changed and volume_endpoint is not None and old_mute is not None:
        try:
            volume_endpoint.SetMute(1 if old_mute else 0, None)
            print("Output mute state restored.")
        except Exception as exc:
            print(f"Audio restore failed: {exc}")
    if awake:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        awake = False
    print("Workflow Mode stopped.", flush=True)


def main():
    global old_brightness, old_mute, volume_endpoint
    global brightness_changed, audio_changed, awake

    parser = argparse.ArgumentParser(description="workflowMode (Windows 10/11)")
    parser.add_argument("--check", action="store_true", help="Check compatible display/audio")
    parser.add_argument("--test", action="store_true", help="30s input block, 20% brightness")
    parser.add_argument("--no-lock", action="store_true", help="Don't intercept input")
    args = parser.parse_args()

    old_brightness = get_brightness()
    volume_endpoint = AudioUtilities.GetSpeakers().EndpointVolume
    old_mute = bool(volume_endpoint.GetMute())
    print(f"Display: {old_brightness}% | Audio muted: {old_mute}", flush=True)
    print("Keyboard backlight is not controlled (hardware-specific Windows drivers).")
    if args.check:
        print("Checks passed. No changes made.")
        return

    timeout = 30 if args.test else DEFAULT_TIMEOUT
    target = 20 if args.test else DEFAULT_BRIGHTNESS
    print(f"Activating in 5 seconds (screen {target}%). Ctrl+C cancels.", flush=True)
    for _ in range(50):
        if not running:
            return
        time.sleep(0.1)

    if not kernel32.SetThreadExecutionState(
        ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    awake = True

    # Install hooks before blackout. If either fails, don't change the display.
    if not args.no_lock:
        install_hooks()

    brightness_changed = True
    set_brightness(target)
    audio_changed = True
    volume_endpoint.SetMute(1, None)
    print("WORKFLOW MODE ACTIVE. Control+C stops and restores settings.", flush=True)
    print("Windows display, sound and awake state remain changed after input timeout.", flush=True)

    deadline = time.monotonic() + timeout
    while running:
        if keyboard_hook and time.monotonic() >= deadline:
            remove_hooks()
            print("Input unlocked. Workflow Mode continues until Ctrl+C.", flush=True)
        if keyboard_hook:
            pump_messages()
        time.sleep(0.02 if keyboard_hook else 0.12)


if __name__ == "__main__":
    if sys.platform != "win32":
        sys.exit("This script requires Windows.")
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Workflow Mode error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        cleanup()
