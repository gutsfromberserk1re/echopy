<div align="center">

<img src="echo-logo.png" alt="Echo logo" width="120">

# Echo

**A tiny voice recorder for Windows.**
Press Space, talk, press Space again. That's it.

<br>

<a href="https://github.com/gutsfromberserk1re/echopy/releases/">
  <img src="https://img.shields.io/badge/%E2%AC%87%20%20Download%20Echo.exe-ed2732?style=for-the-badge&labelColor=1a1e22" alt="Download Echo.exe" height="46">
</a>

<br><br>

<img src="https://img.shields.io/github/actions/workflow/status/gutsfromberserk1re/echopy/build.yml?branch=main&style=flat-square&label=build&labelColor=1a1e22" alt="Build status">
<img src="https://img.shields.io/github/last-commit/gutsfromberserk1re/echopy?style=flat-square&labelColor=1a1e22" alt="Last commit">
<img src="https://img.shields.io/badge/platform-Windows-3a4046?style=flat-square&labelColor=1a1e22" alt="Windows">

</div>

---

## What it is

Echo is a small desktop recorder with a pretty interface. You hit record when you're done you save a normal `.wav` file. No account.

It's built with Python and Qt (PySide6).

## What you can do with it

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

Windows might show a "protected your PC" warning the first time but thats because the app isn't code-signed. Click **More info**, then **Run anyway**.


## Running from source

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
python echo.py
```

