# Icons

These files are **generated**, not hand-drawn, and they are committed so a build
machine does not need a 1024 px source image:

| File | Used by |
|---|---|
| `32x32.png`, `128x128.png`, `128x128@2x.png`, `icon.png` | Tauri window/taskbar |
| `icon.ico` (16-256 px, multi-size) | Windows `.exe`, NSIS and MSI targets |
| `icon.icns` (128-512 px) | macOS bundle (future; not built in 0.82.1) |

Regenerate after any brand change from the repository root:

```powershell
python scripts\make_app_icons.py
```

The script (`scripts/make_app_icons.py`, Pillow) draws the same mark the sidebar
uses - orbital ring, tilted violet ellipse, cyan core - at 8x supersampling and
writes every file above, so the icon set is reproducible from source. Pillow is a
build-machine tool; it is not a runtime dependency of the assistant and is not
needed to run the Command Center.

`npm run tauri:build` fails with a clear "icon not found" message if this
directory is emptied, so a checkout can never ship a missing placeholder icon.
