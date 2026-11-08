# workflowMode

**A lightweight macOS workflow mode for running AI coding tools and long-running jobs while reducing idle display power usage.**

By [opieferd](https://github.com/opieferd).

Keep Claude Code, builds, downloads, and other tools running while the Mac's built-in display is dimmed to **0%**, its keyboard backlight is off, and `caffeinate` prevents idle sleep. Ordinary mouse and keyboard input is filtered for up to 60 minutes to avoid accidental interference.

> This can save **display and keyboard-backlight power**, but it does **not** minimize total system power: AI workloads still use CPU/GPU and network power, and `caffeinate` prevents the Mac from entering idle sleep.

## Features

| Feature | Default | Test mode |
| --- | --- | --- |
| Display brightness | 0% | 20% |
| Keyboard backlight | Off | Off |
| `caffeinate -di` | Until manual exit | Until manual exit |
| Input filtering | 60 minutes | 30 seconds |
| Ctrl+C | Exit and restore | Exit and restore |
| After input timeout | Input unlocks; dimming and caffeinate continue | Same |

**Input blocking is best-effort, not a security lock.** macOS can bypass/disable event taps, and the timer only works while Python is running. Test first and have an independent recovery route.

## Requirements

- macOS, developed on an **M2 MacBook Air** (other models may need adjustments)
- [Homebrew](https://brew.sh/), Python 3, Node.js/npm
- [BetterDisplay](https://betterdisplay.pro/) installed in `/Applications` (uses its built-in CLI; **no full Xcode installation required**)
- [macos-keyboard-backlight](https://www.npmjs.com/package/macos-keyboard-backlight), providing `kbdlight`
- Python package `pyobjc-framework-Quartz`
- Accessibility permission for your terminal; Input Monitoring if prompted

## Full installation

### 1. Clone this repo

```bash
git clone https://github.com/opieferd/workflowMode.git ~/workflow-mode
cd ~/workflow-mode
```

Install Homebrew from [brew.sh](https://brew.sh/) if necessary. Never use `sudo brew`.

### 2. Install dependencies

```bash
brew install python node
brew install --cask betterdisplay
open -a BetterDisplay
npm install -g macos-keyboard-backlight
```

**npm EACCES fix**, if global install fails because `/usr/local/lib/node_modules` is not writable:

```bash
mkdir -p "$HOME/.local"
npm config set prefix "$HOME/.local"
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
npm install -g macos-keyboard-backlight
```

Check: `kbdlight get`.

### 3. Set up the virtual environment

```bash
cd ~/workflow-mode
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 4. Grant system permissions

Go to **System Settings → Privacy & Security → Accessibility**, and enable **Terminal** (or your terminal application). Allow **Input Monitoring** if prompted. Reopen Terminal if the permission does not take effect.


### 5. Add the `workflow` alias (zsh)

```bash
echo 'alias workflow="$HOME/workflow-mode/.venv/bin/python $HOME/workflow-mode/workflow.py"' >> ~/.zshrc
source ~/.zshrc
```

Now use it from **any directory** without activating the virtual environment:

```bash
workflow --check    # Read configuration, change nothing
workflow --test     # Safe trial: 20% screen, 30-second input filter
workflow            # 0% screen, 60-minute input filter
```

**Ctrl+C** requests an exit and restoration. After 60 minutes, only input filtering stops automatically. The display stays dimmed and `caffeinate` keeps running until manual exit.

## How it works

1. Reads your starting screen and keyboard brightness.
2. Creates a macOS Quartz event tap but leaves it off during setup.
3. Starts a 5-second cancel countdown.
4. Runs `caffeinate -di`, dims display/backlight, and filters ordinary keyboard and mouse events.
5. Intercepts **Control+C** (US keyboard C key) to stop. After 60 minutes the input tap automatically disables.
6. On clean exit, unlocks input first, restores brightness, and stops only its own `caffeinate` process.

`caffeinate -di` prevents idle sleep, **not** every kind of sleep (such as closing a MacBook lid or explicitly choosing Sleep). Keep the lid open during unattended tasks.

## Customize

In `workflow.py`:

```python
DEFAULT_INPUT_TIMEOUT = 60 * 60
DEFAULT_SCREEN_BRIGHTNESS = 0.0
DEFAULT_KEYBOARD_BRIGHTNESS = 0.0
DISPLAY_NAME = "Built-in Display"
```

`--test` always uses 30 seconds and 20% screen brightness.

## Safety and troubleshooting

- **Always test `workflow --test` before `workflow`.** Confirm Ctrl+C, the 30-second unlock and correct brightness restoration.
- Have an **independent recovery route** (e.g. SSH or a second display) before using a black screen with input blocked; Ctrl+C is not a guaranteed escape under all failure conditions.
- A forced quit, crash or power interruption might bypass brightness restoration. The input timer only runs while the Python process is alive.
- The event tap is **not** a screen lock or privacy/security control. Some system keyboard shortcuts may remain available.
- For `.../.venv/bin/python: no such file`, recreate the venv using step 3.
- For `kbdlight: command not found`, check npm's prefix and `PATH`.
- For BetterDisplay read errors, start the app, check `get -identifiers`, and update `DISPLAY_NAME` if necessary. Do not remove the fail-safe checks.
- For `Input tap unavailable`, grant Accessibility access to Terminal.
- Some BetterDisplay versions may require permissions or licensing for CLI features. Zero brightness can be software dimming, not true physical backlight-off.

## License

[MIT](LICENSE) · Created by [opieferd](https://github.com/opieferd).
