<p align="center">
  <img src="assets/icon.png" alt="PackDeploy icon" width="128">
</p>

<h1 align="center">PackDeploy</h1>

<p align="center">
  A desktop tool for installing and updating Minecraft <b>Bedrock</b> add-ons on a dedicated server, without rebuilding the world.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-GPLv3-blue" alt="License: GPLv3">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-Windows-lightgrey" alt="Platform: Windows">
</p>

---

Load a `.mcaddon` or `.mcpack`, point PackDeploy at your server's `resource_packs` and `behavior_packs` folders, and click **Deploy**. It extracts each pack to the right place, replaces old versions, and updates the world's pack lists so the server loads them.

## Why

The usual way to add or update packs on a Bedrock server goes like this:

1. Build a world locally with all the packs applied
2. Export it as a `.mcworld` and import it onto the server
3. When an add-on updates, unzip it by hand, figure out which half is which, and overwrite the old folders
4. Edit `world_behavior_packs.json` / `world_resource_packs.json` by hand, or the new version silently fails to load

PackDeploy does all of that in one step.

## Features

- **Reads `.mcaddon`, `.mcpack` and `.zip` files**, including add-ons that contain nested `.mcpack` files
- **Sorts packs automatically** into resource or behavior packs by reading each `manifest.json`, so file and folder names don't matter
- **Updates in place.** Packs already on the server are matched by UUID and replaced in their existing folder, so you never get duplicates
- **Backs up old versions** to `pack_backups/` before replacing them (optional)
- **Updates the world's pack lists.** Version numbers in `world_behavior_packs.json` and `world_resource_packs.json` are bumped to match, and new packs can be added automatically. A `.bak` copy of each file is kept
- **Auto-fill from server folder.** Pick your server root and it finds `resource_packs`, `behavior_packs` and the active world (from `level-name` in `server.properties`)
- **Preview before deploying.** Every pack is listed with its type, version and exactly what will happen (New / Update 1.0.0 → 1.2.0 / Reinstall). You can untick any pack you don't want
- **Handles messy manifests.** It tolerates comments, trailing commas, BOMs and localized pack names (`pack.name` → `texts/en_US.lang`)
- **Remembers your folders** between runs
- **Supports "Open with".** Pass add-on files on the command line, or set PackDeploy as the default app for `.mcaddon` files
- Single file, standard library only, dark-themed UI

## Requirements

- Windows (it should also work on macOS/Linux, but it has only been built for Windows)
- Python 3.10+ with tkinter (included in the standard Windows installer)

No third-party packages are needed.

## Installation

### Run from source

```bash
git clone https://github.com/nullifiedharp/PackDeploy.git
cd PackDeploy
python PackDeploy.py
```

### Build a standalone .exe

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name PackDeploy --icon assets/icon.ico --add-data "assets/icon.ico;assets" PackDeploy.py
```

`--icon` sets the .exe's icon in Explorer, and `--add-data` bundles the icon so the app window and taskbar use it too.

The executable will be in `dist/PackDeploy.exe`.

To open add-ons by double-clicking them: right-click any `.mcaddon` → **Open with** → **Choose another app** → browse to `PackDeploy.exe` → tick **Always use this app**.

## Usage

1. **Stop the server.** Windows locks pack files while it's running.
2. Click **Auto-fill from server folder…** and select your Bedrock server's root folder. You can also set each folder manually:
   - **Resource packs folder:** `<server>/resource_packs`
   - **Behavior packs folder:** `<server>/behavior_packs`
   - **World folder (optional):** `<server>/worlds/<world name>`
3. Click **Browse…** next to *Add-on file(s)* and select one or more add-ons.
4. Check the pack list and untick anything you don't want.
5. Click **Deploy to server**.
6. Start the server.

### Options

| Option | Default | What it does |
|---|---|---|
| Back up replaced packs | On | Moves the old version to `<server>/pack_backups/<folder>/<pack>_v<version>_<timestamp>` instead of deleting it |
| Add new packs to world | On | Adds packs that aren't yet in the world's pack lists. When off, only existing entries get version bumps |

If no world folder is set, packs are only copied into the pack folders and the world files are not touched.

## How it works

```
my_addon.mcaddon
├── Cool RP.mcpack   ──► manifest: "resources" module ──► resource_packs/Cool RP/
└── Cool BP.mcpack   ──► manifest: "data"/"script"   ──► behavior_packs/Cool BP/
                                                          │
                     worlds/<name>/world_resource_packs.json  ◄─ pack_id + version
                     worlds/<name>/world_behavior_packs.json  ◄─ pack_id + version
```

- **Pack type** comes from the manifest's `modules[].type`: `resources` → resource pack; `data`, `script`, `javascript`, `client_data` → behavior pack. Skin packs and world templates are listed but can't be deployed.
- **Existing packs** are found by scanning every folder in the target directory for a `manifest.json` with the same header UUID.
- **Extraction** goes to a temporary folder first and is then swapped in, so a failed extraction never leaves a half-written pack. Archive paths that try to escape the target folder are skipped.

## Notes and limitations

- **Packs embedded in the world folder.** Worlds exported as `.mcworld` often contain their own `behavior_packs/` and `resource_packs/` folders inside `worlds/<name>/`. These can take priority over the server-level folders. Either point PackDeploy at those folders or remove them and use the server-level ones.
- **Experimental toggles.** Add-ons that need Beta APIs or other experiments must have them enabled in the world's `level.dat`. PackDeploy doesn't edit `level.dat`.
- **Load order.** New packs are appended to the end of the world's pack list, which gives them the lowest priority. If two packs change the same thing, reorder the JSON by hand.
- **Restart required.** The server only reads pack changes on startup.
- **Bedrock only.** Java Edition resource packs and datapacks use a different format.

## License

Copyright (C) 2026 NullHarp

PackDeploy is free software: you can redistribute it and/or modify it under the terms of the [GNU General Public License v3.0](LICENSE) as published by the Free Software Foundation.

In short: you're free to use, modify and share it, but any version you distribute must also be released under GPLv3 with its source code, and must keep the original copyright notice.
