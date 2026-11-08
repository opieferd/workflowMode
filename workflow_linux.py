#!/usr/bin/env python3
"""workflowMode for Linux: backlight, audio mute, idle inhibition and timed X11 input grab.

X11 input filtering is best-effort and optional. Wayland intentionally does NOT
attempt a global input grab because a normal application cannot safely do so.
"""
import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time

DEFAULT_TIMEOUT = 3600
DEFAULT_BRIGHTNESS_PERCENT = 0
running = True
inhibitor = None
old_brightness = None
keyboard_device = None
old_keyboard = None
audio_backend = None
old_mute = None
screen_changed = False
keyboard_changed = False
audio_changed = False
display = None
keyboard_grabbed = False
pointer_grabbed = False


def run(*args):
    return subprocess.run(
        [str(arg) for arg in args], capture_output=True, text=True,
        check=True, timeout=10
    ).stdout.strip()


def get_screen():
    return int(run("brightnessctl", "-c", "backlight", "get"))


def set_screen(level):
    run("brightnessctl", "-c", "backlight", "-n", "0", "set", str(level))


def get_keyboard_device():
    lines = run("brightnessctl", "--list", "--machine-readable").splitlines()
    for line in lines:
        pieces = line.split(",")
        if len(pieces) > 1 and pieces[1].strip().lower() == "leds":
            device = pieces[0].strip()
            if "kbd_backlight" in device or "keyboard" in device.lower():
                return device
    return None


def get_keyboard(device):
    return int(run("brightnessctl", "-d", device, "get"))


def set_keyboard(device, level):
    run("brightnessctl", "-d", device, "-n", "0", "set", str(level))


def detect_audio_backend():
    if shutil.which("wpctl"):
        try:
            status = run("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@")
            if "Volume:" in status:
                return "wpctl", "[MUTED]" in status.upper()
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    if shutil.which("pactl"):
        status = run("pactl", "get-sink-mute", "@DEFAULT_SINK@")
        if re.search(r"\b(?:yes|no)\b", status, re.I):
            return "pactl", bool(re.search(r"\byes\b", status, re.I))
    raise RuntimeError("No working wpctl/PipeWire or pactl/PulseAudio output mute interface.")


def set_mute(value):
    if audio_backend == "wpctl":
        run("wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@", "1" if value else "0")
    else:
        run("pactl", "set-sink-mute", "@DEFAULT_SINK@", "1" if value else "0")


def stop(*_):
    global running
    running = False


signal.signal(signal.SIGINT, stop)
signal.signal(signal.SIGTERM, stop)


def prepare_x11():
    global display
    if os.environ.get("XDG_SESSION_TYPE", "").lower() != "x11":
        print("Wayland/unknown session: global input filtering unsupported; proceeding without it.")
        return False
    if not os.environ.get("DISPLAY"):
        print("No X11 DISPLAY: input filtering skipped.")
        return False
    try:
        from Xlib import display as xdisplay
        display = xdisplay.Display()
    except Exception as exc:
        print(f"X11 input filter unavailable ({exc}); proceeding without it.")
        return False
    return True


def grab_x11():
    global keyboard_grabbed, pointer_grabbed
    from Xlib import X
    root = display.screen().root
    keyboard_status = root.grab_keyboard(
        owner_events=False, pointer_mode=X.GrabModeAsync,
        keyboard_mode=X.GrabModeAsync, time=X.CurrentTime
    )
    if keyboard_status != X.GrabSuccess:
        raise RuntimeError("Failed to grab X11 keyboard; refusing to dim with a partial grab.")
    keyboard_grabbed = True
    pointer_status = root.grab_pointer(
        owner_events=False,
        event_mask=X.ButtonPressMask | X.ButtonReleaseMask | X.PointerMotionMask,
        pointer_mode=X.GrabModeAsync, keyboard_mode=X.GrabModeAsync,
        confine_to=X.NONE, cursor=X.NONE, time=X.CurrentTime
    )
    if pointer_status != X.GrabSuccess:
        ungrab_x11()
        raise RuntimeError("Failed to grab X11 pointer; released keyboard grab.")
    pointer_grabbed = True
    display.flush()


def ungrab_x11():
    global keyboard_grabbed, pointer_grabbed
    if display is None:
        return
    if keyboard_grabbed or pointer_grabbed:
        from Xlib import X
        if keyboard_grabbed:
            display.ungrab_keyboard(X.CurrentTime)
            keyboard_grabbed = False
        if pointer_grabbed:
            display.ungrab_pointer(X.CurrentTime)
            pointer_grabbed = False
        display.flush()


def pump_x11():
    global running
    from Xlib import X, XK
    while display.pending_events():
        event = display.next_event()
        if event.type == X.KeyPress and event.state & X.ControlMask:
            keysym = display.keycode_to_keysym(event.detail, 0)
            if keysym in (XK.string_to_keysym("c"), XK.string_to_keysym("C")):
                running = False


def cleanup():
    global inhibitor, display
    # Release all input interception BEFORE trying any potentially failing commands.
    try:
        ungrab_x11()
    except Exception as exc:
        print(f"Warning: couldn't release X11 input grabs: {exc}", flush=True)
    if display is not None:
        try:
            display.close()
        except Exception:
            pass
        display = None

    if screen_changed and old_brightness is not None:
        try:
            set_screen(old_brightness)
            print("Screen brightness restored.")
        except Exception as exc:
            print(f"Screen restore failed: {exc}")
    if keyboard_changed and keyboard_device is not None and old_keyboard is not None:
        try:
            set_keyboard(keyboard_device, old_keyboard)
            print("Keyboard backlight restored.")
        except Exception as exc:
            print(f"Keyboard restore failed: {exc}")
    if audio_changed and old_mute is not None:
        try:
            set_mute(old_mute)
            print("Original audio mute state restored.")
        except Exception as exc:
            print(f"Audio restore failed: {exc}")

    if inhibitor is not None and inhibitor.poll() is None:
        # Terminate the whole inhibitor command group, including sleep infinity.
        try:
            os.killpg(inhibitor.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            inhibitor.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(inhibitor.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            inhibitor.wait()
    print("Workflow Mode stopped.", flush=True)


def main():
    global inhibitor, old_brightness, keyboard_device, old_keyboard, audio_backend
    global old_mute, screen_changed, keyboard_changed, audio_changed
    parser = argparse.ArgumentParser(description="workflowMode (Linux)")
    parser.add_argument("--check", action="store_true", help="Check dependencies without changing state")
    parser.add_argument("--test", action="store_true", help="30s input timeout, 20% brightness")
    parser.add_argument("--no-lock", action="store_true", help="Never grab keyboard or mouse")
    args = parser.parse_args()

    if not shutil.which("brightnessctl"):
        raise RuntimeError("brightnessctl not installed.")
    if not shutil.which("systemd-inhibit"):
        raise RuntimeError("systemd-inhibit not available; requires systemd/logind.")

    old_brightness = get_screen()
    maximum = int(run("brightnessctl", "-c", "backlight", "max"))
    if maximum <= 0:
        raise RuntimeError("No valid backlight brightness maximum.")
    keyboard_device = get_keyboard_device()
    if keyboard_device:
        old_keyboard = get_keyboard(keyboard_device)
    audio_backend, old_mute = detect_audio_backend()
    print(f"Screen: {old_brightness}/{maximum} | Keyboard: {old_keyboard if keyboard_device else 'unavailable'} | Muted: {old_mute}")
    if args.check:
        print("Checks passed. No changes made.")
        return

    can_grab = not args.no_lock and prepare_x11()
    timeout = 30 if args.test else DEFAULT_TIMEOUT
    target = round(maximum * (0.20 if args.test else DEFAULT_BRIGHTNESS_PERCENT / 100.0))
    print(f"Starting in 5 seconds. Ctrl+C cancels. Input grab: {'X11 for ' + str(timeout) + 's' if can_grab else 'not supported / disabled'}.")
    for _ in range(50):
        if not running:
            return
        time.sleep(0.1)

    inhibitor = subprocess.Popen(
        ["systemd-inhibit", "--what=idle:sleep", "--mode=block",
         "--why=workflowMode is running long-lived AI tasks", "sleep", "infinity"],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        start_new_session=True
    )
    time.sleep(0.15)
    if inhibitor.poll() is not None:
        raise RuntimeError(f"systemd-inhibit failed: {inhibitor.stderr.read()}")

    keyboard_changed = bool(keyboard_device)
    if keyboard_device:
        set_keyboard(keyboard_device, 0)
    screen_changed = True
    set_screen(target)
    audio_changed = True
    set_mute(True)

    if can_grab:
        grab_x11()
        print("X11 input grabbed. Ctrl+C exits, or input unlocks after timeout.")
    else:
        print("Input stays available. Dim screen + muted audio + idle inhibition are active.")
    deadline = time.monotonic() + timeout
    while running:
        if keyboard_grabbed and time.monotonic() >= deadline:
            ungrab_x11()
            print("Input unlocked. Workflow Mode remains active until Ctrl+C.")
        elif keyboard_grabbed:
            pump_x11()
        time.sleep(0.04 if keyboard_grabbed else 0.1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Workflow Mode error: {exc}", file=sys.stderr, flush=True)
        sys.exit(1)
    finally:
        cleanup()
