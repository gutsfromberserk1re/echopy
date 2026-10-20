<div align="center">

<img src="echo-logo.png" alt="Echo logo" width="120">

# Echo

**A tiny, good-looking voice recorder for Windows.**
Press Space, talk, press Space again. That's it.

<br>

<a href="https://github.com/YOUR_USERNAME/YOUR_REPO/releases/latest/download/Echo.exe">
  <img src="https://img.shields.io/badge/%E2%AC%87%20%20Download%20Echo.exe-ed2732?style=for-the-badge&labelColor=1a1e22" alt="Download Echo.exe" height="46">
</a>

<br><br>

<img src="https://img.shields.io/github/actions/workflow/status/YOUR_USERNAME/YOUR_REPO/build.yml?branch=main&style=flat-square&label=build&labelColor=1a1e22" alt="Build status">
<img src="https://img.shields.io/github/last-commit/YOUR_USERNAME/YOUR_REPO?style=flat-square&labelColor=1a1e22" alt="Last commit">
<img src="https://img.shields.io/badge/platform-Windows-3a4046?style=flat-square&labelColor=1a1e22" alt="Windows">

</div>

---

## What it is

Echo is a small desktop recorder with a frosted-glass window. You hit record, a waveform scrolls past a little pink playhead while you talk, and when you're done you save a normal `.wav` file. No account, no cloud, no settings maze.

It's built with Python and Qt (PySide6), and it works nicely with virtual audio devices like Voicemeeter, which is the thing most simple recorders get wrong.

## What you can do with it

- **Record and pause** as many times as you like. Everything lands in one take.
- **Play it back** and click or drag the waveform to jump around.
- **Open an existing WAV** (8, 16, 24 or 32-bit) and keep recording onto the end of it.
- **Pick your microphone** from the menu. Echo prefers a real mic, but you can choose any input, virtual ones included.
- **Don't lose work.** The audio is quietly saved to disk while you record, so if Echo or your PC crashes, it offers to bring the recording back next time you open it.
- **Save safely.** Saving never overwrites your file until the new one is fully written.

## Keyboard shortcuts

| Key | What it does |
| --- | --- |
| `Space` | Record / pause |
| `P` | Play / pause |
| `←` `→` | Skip 5 seconds |
| `Shift+S` or `Ctrl+S` | Save |
| `Ctrl+Shift+S` | Save as |

## Getting it

Click the big red **Download Echo.exe** button at the top. It's a single file, so there's nothing to install. Just double-click it.

Windows might show a "protected your PC" warning the first time, because the app isn't code-signed. Click **More info**, then **Run anyway**.

The download always points at the newest build. Every time new code is pushed to this repo, GitHub builds a fresh `Echo.exe` and replaces the old one automatically.

## Running from source

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
python echo.py
```

`sounddevice` is optional but recommended. Without it Echo falls back to Qt's audio, which has a harder time with Voicemeeter and similar devices.

## How the automatic build works

The file `.github/workflows/build.yml` does this on every push to `main`:

1. Starts a Windows machine on GitHub.
2. Installs Python and the dependencies from `requirements.txt`.
3. Compiles `echo.py` into a single `Echo.exe` using [Nuitka](https://nuitka.net).
4. Publishes it as the **latest** release, which is what the download button links to.

You can also trigger a build by hand from the **Actions** tab (**Build Echo**, then **Run workflow**).

The icon comes from `recorder.ico` next to `echo.py`. The build bakes it into the exe and the app uses it in its own window too.

## Settings you might want to change

At the top of `echo.py` there's a small block of tweakables: glass tint, bar size, scroll speed, and `PREFERRED_INPUT`, the microphone Echo picks at startup (leave it empty to auto-pick a real mic).
