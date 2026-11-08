#!/usr/bin/env python3
"""workflowMode: dim a Mac while long-running jobs keep working.

Input blocking is best-effort, not a security lock. Test safe mode and
ensure an independent recovery path before using blackout mode.
"""
import argparse
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

import Quartz

DEFAULT_INPUT_TIMEOUT = 60 * 60
DEFAULT_SCREEN_BRIGHTNESS = 0.0
DEFAULT_KEYBOARD_BRIGHTNESS = 0.0
DISPLAY_NAME = "Built-in Display"
BETTERDISPLAY = Path("/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay")

running = True
input_locked = False
tap = None
source = None
caffeinate = None
old_screen = None
old_keyboard = None
old_auto = None
old_mute = None
audio_changed = False
screen_changed = False
keyboard_changed = False


def command(*args):
    result = subprocess.run(
        [str(value) for value in args],
        capture_output=True, text=True, check=True, timeout=15
    )
    return result.stdout.strip()


def screen_brightness_from_output(output):
    match = re.search(
        r"brightness\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)\s*(%)?", output, re.I
    )
    if not match:
        match = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(%)?\s*", output)
    if not match:
        raise RuntimeError(f"Unrecognized BetterDisplay brightness output: {output!r}")
    brightness = float(match.group(1))
    if match.group(2) or brightness > 1:
        brightness /= 100
    if not 0 <= brightness <= 1:
        raise RuntimeError(f"Out-of-range brightness value: {brightness}")
    return brightness


def get_screen():
    value = command(BETTERDISPLAY, "get", f"-name={DISPLAY_NAME}", "-brightness")
    return screen_brightness_from_output(value)


def set_screen(level):
    command(BETTERDISPLAY, "set", f"-name={DISPLAY_NAME}", f"-brightness={level:.4f}")


def get_keyboard():
    output = command("kbdlight", "get")
    match = re.search(r"\d+(?:\.\d+)?", output)
    if not match:
        raise RuntimeError(f"Cannot read keyboard backlight from {output!r}")
    return float(match.group())


def set_keyboard(level):
    command("kbdlight", "set", str(level))


def get_auto():
    try:
        output = command("kbdlight", "auto")
        if re.search(r"\b(?:on|enabled|true)\b", output, re.I):
            return True
        if re.search(r"\b(?:off|disabled|false)\b", output, re.I):
            return False
        if output.strip() in ("1", "0"):
            return output.strip() == "1"
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def get_output_mute():
    """Read AppleScript output mute status without changing volume."""
    value = command("osascript", "-e", "output muted of (get volume settings)")
    if value.lower() not in ("true", "false"):
        raise RuntimeError(f"Unexpected macOS output mute state: {value!r}")
    return value.lower() == "true"


def set_output_mute(muted):
    command("osascript", "-e", f"set volume output muted {'true' if muted else 'false'}")


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)

EVENTS = [
    Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp,
    Quartz.kCGEventFlagsChanged, Quartz.kCGEventMouseMoved,
    Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
    Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
    Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
    Quartz.kCGEventLeftMouseDragged, Quartz.kCGEventRightMouseDragged,
    Quartz.kCGEventOtherMouseDragged, Quartz.kCGEventScrollWheel,
]
MASK = sum(1 << kind for kind in EVENTS)


def filter_input(_proxy, event_type, event, _refcon):
    global running
    if event_type in (Quartz.kCGEventTapDisabledByTimeout,
                      Quartz.kCGEventTapDisabledByUserInput):
        if input_locked and tap is not None:
            Quartz.CGEventTapEnable(tap, True)
        return None

    if event_type == Quartz.kCGEventKeyDown:
        keycode = Quartz.CGEventGetIntegerValueField(
            event, Quartz.kCGKeyboardEventKeycode
        )
        flags = Quartz.CGEventGetFlags(event)
        control = bool(flags & Quartz.kCGEventFlagMaskControl)
        command_key = bool(flags & Quartz.kCGEventFlagMaskCommand)
        # Hardware keycode 8 = C on standard US keyboard layouts.
        if keycode == 8 and control and not command_key:
            running = False
            return None

    if event_type == Quartz.kCGEventFlagsChanged:
        return event
    return None


def unlock():
    global input_locked
    input_locked = False
    if tap is not None:
        Quartz.CGEventTapEnable(tap, False)


def cleanup():
    unlock()
    if audio_changed and old_mute is not None:
        try:
            set_output_mute(old_mute)
            print("Audio mute state restored.", flush=True)
        except Exception as exc:
            print(f"Could not restore audio mute state: {exc}", flush=True)
    if screen_changed and old_screen is not None:
        try:
            set_screen(old_screen)
            print("Display brightness restored.", flush=True)
        except Exception as exc:
            print(f"Could not restore display brightness: {exc}", flush=True)

    if keyboard_changed and old_keyboard is not None:
        try:
            set_keyboard(old_keyboard)
            if old_auto is not None:
                command("kbdlight", "auto", "on" if old_auto else "off")
            print("Keyboard backlight restored.", flush=True)
        except Exception as exc:
            print(f"Could not restore keyboard backlight: {exc}", flush=True)

    if caffeinate is not None and caffeinate.poll() is None:
        caffeinate.terminate()
        try:
            caffeinate.wait(timeout=5)
        except subprocess.TimeoutExpired:
            caffeinate.kill()
            caffeinate.wait()

    if source is not None:
        Quartz.CFRunLoopRemoveSource(
            Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
        )
    print("Workflow Mode stopped.", flush=True)


def main():
    global tap, source, caffeinate, old_screen, old_keyboard, old_auto
    global screen_changed, keyboard_changed, input_locked, old_mute, audio_changed

    parser = argparse.ArgumentParser(
        description="Idle display dimming + caffeinate + timed input lock on macOS"
    )
    parser.add_argument("--test", action="store_true",
                        help="30-second lock, 20% display brightness")
    parser.add_argument("--check", action="store_true",
                        help="Check dependencies and read brightness without locking input")
    args = parser.parse_args()
    timeout = 30 if args.test else DEFAULT_INPUT_TIMEOUT
    screen_level = 0.20 if args.test else DEFAULT_SCREEN_BRIGHTNESS

    if not BETTERDISPLAY.is_file():
        raise RuntimeError("BetterDisplay not found in /Applications. Install and open it first.")
    if not shutil.which("kbdlight"):
        raise RuntimeError("kbdlight not found on PATH.")

    old_screen = get_screen()
    old_keyboard = get_keyboard()
    old_auto = get_auto()
    old_mute = get_output_mute()
    print(f"Display: {old_screen:.2f} | Keyboard: {old_keyboard:.2f} | Audio muted: {old_mute}", flush=True)
    if args.check:
        print("Dependency checks passed; no changes made.")
        return

    tap = Quartz.CGEventTapCreate(
        Quartz.kCGHIDEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionDefault,
        MASK, filter_input, None
    )
    if tap is None:
        raise RuntimeError(
            "Input tap unavailable. Allow Terminal/Python in Accessibility permissions."
        )

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    Quartz.CFRunLoopAddSource(
        Quartz.CFRunLoopGetCurrent(), source, Quartz.kCFRunLoopCommonModes
    )
    unlock()

    print(f"Activating in 5 s. Ctrl+C cancels. Input lock: {timeout} s.", flush=True)
    for _ in range(50):
        if not running:
            return
        time.sleep(0.1)

    caffeinate = subprocess.Popen(["caffeinate", "-di"])
    keyboard_changed = True
    if old_auto is not None:
        command("kbdlight", "auto", "off")
    set_keyboard(DEFAULT_KEYBOARD_BRIGHTNESS)
    screen_changed = True
    set_screen(screen_level)
    audio_changed = True
    set_output_mute(True)

    deadline = time.monotonic() + timeout
    input_locked = True
    Quartz.CGEventTapEnable(tap, True)
    print("WORKFLOW MODE ACTIVE. Ctrl+C stops and restores settings.", flush=True)
    print("Input unlocks on timeout; display, audio mute and caffeinate continue.", flush=True)

    while running:
        if input_locked and time.monotonic() >= deadline:
            unlock()
            print("Input unlocked; Workflow Mode continues until Ctrl+C.", flush=True)
        elif input_locked:
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.1, False)
        else:
            time.sleep(0.1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Workflow Mode error: {exc}", flush=True)
        raise SystemExit(1)
    finally:
        cleanup()
