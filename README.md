<p align="center">
  <img src="assets/icon.png" alt="PackDeploy icon" width="128">
</p>

<h1 align="center">PackDeploy</h1>

<p align="center">
  A desktop tool for installing, updating, removing and repairing Minecraft <b>Bedrock</b> add-ons on a dedicated server, without rebuilding the world.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/license-GPLv3-blue" alt="License: GPLv3">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/platform-Windows-lightgrey" alt="Platform: Windows">
</p>

---

Load a `.mcaddon` or `.mcpack`, point PackDeploy at your server's `resource_packs` and `behavior_packs` folders, and click **Deploy**. It extracts each pack to the right place, replaces old versions, and updates the world's pack lists so the server loads them.

It can also remove packs cleanly, and its built-in **PackDoctor** checks old add-ons for the things that break them on newer Minecraft versions, fixing the simple ones automatically.

## Why

The usual way to add or update packs on a Bedrock server goes like this:

1. Build a world locally with all the packs applied
2. Export it as a `.mcworld` and import it onto the server
3. When an add-on updates, unzip it by hand, figure out which half is which, and overwrite the old folders
4. Edit `world_behavior_packs.json` / `world_resource_packs.json` by hand, or the new version silently fails to load

Removing a pack means deleting both halves and cleaning the world files by hand. And when an add-on stops being updated, it usually breaks after a Minecraft update with no clue why.

PackDeploy handles all of that from one window.

## Features

### Deploy

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

### Installed

- **Lists every pack on the server** with its type, version, folder, and whether the world has it enabled
- **Remove packs cleanly.** Deletes the pack folder and removes it from the world's pack lists. If the pack is half of an add-on, PackDeploy offers to remove the other half too, and warns you if something else still depends on it
- **Enable or disable in world** without deleting anything, which is handy for testing which pack is causing problems
- Removed packs go to `pack_backups/` unless backups are turned off

### PackDoctor

Scans a loaded add-on, an installed pack, or any `.mcaddon` file for known compatibility problems. It runs automatically whenever you load an add-on.

| Problem | What PackDoctor does |
|---|---|
| Renamed or restructured block components (`destroy_time`, `explosion_resistance`, `block_light_emission`, `entity_collision`, `pick_collision`, `unit_cube`, `rotation`, `creative_category`, ...) | **Auto-fix:** converts to the current component and format |
| `description.properties` and `query.block_property` | **Auto-fix:** renames to `states` / `query.block_state` |
| Removed components (`breakonpush`, `immovable`, ...) and `is_experimental` flags | **Auto-fix:** removes them |
| Duplicate module UUIDs, versions written as text in `manifest.json` | **Auto-fix** |
| Holiday Creator Features (`on_interact`, `on_placed`, `events`, ...). This is the usual reason old furniture packs break | **Flags it:** must be rewritten as Custom Components with a script |
| Beta Script API versions (`@minecraft/server 1.x.0-beta`) or old `mojang-minecraft` script names | **Flags it**, with how to update |
| Invalid JSON, missing dependency packs, very old `min_engine_version` | **Flags it** |

Fixes are never applied to the original file:

- **Export fixed copy…** saves a new `.mcaddon` that you can load and deploy straight away
- **Fix installed pack(s) in place** copies the pack to `pack_backups/` first, then fixes it on the server

## Requirements

- Windows (it should also work on macOS/Linux, but it has only been built for Windows)
- **Installer:** nothing else. Python isn't needed
- **From source:** Python 3.10+ with tkinter (included in the standard Windows Python installer). No third-party packages are needed

## Installation

### Installer (recommended)

1. Download `PackDeploy-<version>-Setup.exe` from the [latest release](https://github.com/nullifiedharp/PackDeploy/tree/main/releases/latest).
2. Run it and follow the prompts. No admin rights are needed.
3. Optionally, tick **Open .mcaddon and .mcpack files with PackDeploy**. This adds a **Deploy with PackDeploy** option to the right-click menu for add-on files.

Windows SmartScreen may warn that the installer is from an unknown publisher, because it isn't code-signed. Click **More info → Run anyway**.

To update, run the newer installer and it will replace the old version. To uninstall, use **Settings → Apps**.

### Run from source

Requires Python 3.10+.

```bash
git clone https://github.com/nullifiedharp/PackDeploy.git
cd PackDeploy
python PackDeploy.py
```

### Build it yourself

Run `build.bat` from the repo folder (optionally with a version number, e.g. `build.bat 1.2.0`). It will:

1. Install PyInstaller and build `dist\PackDeploy.exe`, a portable single-file app
2. Install Inno Setup 6 through winget if it isn't already installed
3. Compile `installer\PackDeploy-<version>-Setup.exe` from `installer.iss`

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

### Removing a pack

1. Stop the server and open the **Installed** tab.
2. Select the pack(s) and click **Remove selected…**.
3. Confirm. If the pack has a linked resource/behavior half, choose whether to remove it too.

### Fixing an old pack

1. Load the add-on, or select it on the **Installed** tab and click **Check with PackDoctor**.
2. Click an issue to see what it means and how to fix it.
3. Click **Export fixed copy…** or **Fix installed pack(s) in place**.
4. Start the server and check the content log (`content-log-file-enabled=true` in `server.properties`) for anything left over. Anything marked *Error* in PackDoctor still needs manual work.

### Options

| Option | Default | What it does |
|---|---|---|
| Back up replaced / removed packs | On | Moves old or removed packs to `<server>/pack_backups/<folder>/<pack>_v<version>_<timestamp>` instead of deleting them |
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
- **PackDoctor isn't a full converter.** It fixes known renames and flags known breakages, but it can't rewrite Holiday Creator Features logic or update scripts, and it doesn't check every component. The content log is the final word.
- **Fixing someone else's pack.** Patching an add-on for your own server is fine. Sharing a fixed version publicly needs the original creator's permission, unless their license allows it.
- **Restart required.** The server only reads pack changes on startup.
- **Bedrock only.** Java Edition resource packs and datapacks use a different format.

## License

Copyright (C) 2026 NullHarp

PackDeploy is free software: you can redistribute it and/or modify it under the terms of the [GNU General Public License v3.0](LICENSE) as published by the Free Software Foundation.

In short: you're free to use, modify and share it, but any version you distribute must also be released under GPLv3 with its source code, and must keep the original copyright notice.
