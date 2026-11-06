#!/usr/bin/env python3
"""
PackDeploy — install, update, remove and repair Minecraft Bedrock add-ons on a
dedicated server.

  Deploy     Load .mcaddon / .mcpack / .zip files and extract each pack into the
             server's resource_packs / behavior_packs folders. Installed packs
             (matched by manifest UUID) are replaced in place, with an optional
             backup, and the world's pack lists are updated.
  Installed  See every pack on the server, enable/disable it in the world, or
             remove it (and its linked resource/behavior half) completely.
  PackDoctor Scan packs for things that break them on newer Minecraft versions,
             auto-fix the safe ones, and explain the rest.

Standard library only. Python 3.10+.

Copyright (C) 2026 NullHarp

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
import copy
import io
import json
import os
import queue
import re
import shutil
import stat
import sys
import threading
import time
import uuid as uuidlib
import zipfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP = "PackDeploy"
APP_VERSION = "1.1.0"
CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

BG = "#1e1e1e"
PANEL = "#252526"
FIELD = "#2d2d30"
BORDER = "#3c3c3c"
FG = "#e0e0e0"
DIM = "#8a8a8a"
ACCENT = "#3a7bd5"
GOOD = "#6cc070"
WARN = "#e0b050"
BAD = "#e06060"
INFO = "#7fb4ff"

KIND_LABEL = {
    "resource": "Resource",
    "behavior": "Behavior",
    "skin": "Skin pack",
    "world": "World template",
    "unknown": "Unknown",
}
DEPLOYABLE = ("resource", "behavior")
WORLD_FILES = {"behavior": "world_behavior_packs.json", "resource": "world_resource_packs.json"}


# --------------------------------------------------------------------------- #
#  JSON / misc helpers (Bedrock files often have comments / trailing commas)
# --------------------------------------------------------------------------- #
def strip_json_comments(text):
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def loads_lenient(data):
    if isinstance(data, bytes):
        data = data.decode("utf-8-sig", errors="replace")
    data = data.lstrip("﻿")
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        pass
    cleaned = strip_json_comments(data)
    cleaned = re.sub(r",(\s*[}\]])", r"\1", cleaned)
    return json.loads(cleaned)


def read_json_file(path, default):
    if not os.path.exists(path):
        return default
    with open(path, "rb") as f:
        raw = f.read()
    if not raw.strip():
        return default
    return loads_lenient(raw)


def ver_str(v):
    if isinstance(v, (list, tuple)):
        return ".".join(str(x) for x in v)
    return "?" if v is None else str(v)


def vtuple(v):
    """'1.16.100' / [1,16,100] -> (1,16,100); anything unparseable -> (0,)."""
    if isinstance(v, (list, tuple)):
        try:
            return tuple(int(x) for x in v)
        except (TypeError, ValueError):
            return (0,)
    if isinstance(v, (int, float)):
        return (int(v),)
    if isinstance(v, str):
        parts = re.findall(r"\d+", v.split("-")[0])
        return tuple(int(x) for x in parts) or (0,)
    return (0,)


def parse_semver(s):
    m = re.fullmatch(r"\s*(\d+)\.(\d+)\.(\d+)\s*", s or "")
    return [int(m.group(1)), int(m.group(2)), int(m.group(3))] if m else None


def is_uuid(s):
    try:
        uuidlib.UUID(str(s))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def safe_folder_name(name, pack_uuid):
    name = re.sub(r"§.", "", name or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:60].strip(" .")
    return name or f"pack_{pack_uuid[:8]}"


def resolve_pack_name(raw, lang_bytes, fallback):
    name = raw or ""
    if name.startswith("pack.") and lang_bytes:  # localized, e.g. "pack.name"
        for line in lang_bytes.decode("utf-8-sig", "replace").splitlines():
            if line.startswith(name + "="):
                name = line.split("=", 1)[1].split("\t")[0].split("##")[0].strip()
                break
    name = re.sub(r"§.", "", name).strip()
    if not name or name.startswith("pack."):
        name = fallback
    return name


def manifest_dep_uuids(m):
    out = set()
    for d in m.get("dependencies", []) or []:
        if isinstance(d, dict) and d.get("uuid"):
            out.add(str(d["uuid"]).lower())
    return out


def detect_kind(manifest):
    types = {
        str(m.get("type", "")).lower()
        for m in manifest.get("modules", []) or []
        if isinstance(m, dict)
    }
    if "resources" in types:
        return "resource"
    if types & {"data", "script", "javascript", "client_data"}:
        return "behavior"
    if "skin_pack" in types:
        return "skin"
    if "world_template" in types:
        return "world"
    return "unknown"


def _rm_readonly(func, path, *_):
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def rmtree(path):
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_rm_readonly)
    else:
        shutil.rmtree(path, onerror=_rm_readonly)


def backup_root_for(packs_dir):
    """<server>/pack_backups/<resource_packs|behavior_packs>"""
    parent = os.path.dirname(os.path.abspath(packs_dir))
    return os.path.join(parent, "pack_backups", os.path.basename(os.path.normpath(packs_dir)))


def backup_file(path, log):
    """Plain byte copy (no permission/timestamp copying, which SMB/NAS shares often
    refuse). Never fatal: a failed backup is a warning, not a reason to abort."""
    candidates = [path + ".bak", f"{path}.{time.strftime('%Y%m%d-%H%M%S')}.bak"]
    last = None
    for bak in candidates:
        try:
            if os.path.exists(bak):
                try:
                    os.chmod(bak, 0o666)  # clear read-only so it can be replaced
                except OSError:
                    pass
            shutil.copyfile(path, bak)
            return bak
        except OSError as e:
            last = e
    log(f"  ⚠ Couldn't back up {os.path.basename(path)} ({last}) — continuing without a backup", "warn")
    return None


# --------------------------------------------------------------------------- #
#  Packs inside archives
# --------------------------------------------------------------------------- #
class Pack:
    def __init__(self, zf, prefix, manifest, source):
        self.zf = zf
        self.prefix = prefix
        self.source = source
        header = manifest.get("header", {}) or {}
        self.uuid_raw = str(header.get("uuid", ""))
        self.uuid = self.uuid_raw.lower()
        self.version = header.get("version")
        self.kind = detect_kind(manifest)
        self.deps = manifest_dep_uuids(manifest)
        folder = os.path.basename(self.prefix.rstrip("/"))
        fallback = folder or os.path.splitext(self.source.split(" › ")[-1])[0]
        self.name = resolve_pack_name(str(header.get("name", "") or ""),
                                      self.read_member("texts/en_US.lang"), fallback)
        self.include = self.kind in DEPLOYABLE
        # filled in by plan()
        self.target_dir = None
        self.target_folder = None
        self.old_version = None
        self.status = ""

    def read_member(self, rel):
        target = (self.prefix + rel).lower()
        for info in self.zf.infolist():
            if info.filename.replace("\\", "/").lower() == target:
                return self.zf.read(info)
        return None

    def members(self):
        for info in self.zf.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/") or not name.startswith(self.prefix):
                continue
            rel = name[len(self.prefix):]
            if rel:
                yield info, rel


def scan_archive(zf, source, depth=0):
    packs, warnings = [], []
    manifests = []
    for info in zf.infolist():
        n = info.filename.replace("\\", "/")
        low = n.lower()
        if low == "manifest.json" or low.endswith("/manifest.json"):
            manifests.append((n[: -len("manifest.json")], info))
    manifests.sort(key=lambda x: len(x[0]))

    roots = []
    for prefix, info in manifests:
        if any(prefix.startswith(r) for r in roots):
            continue  # inside a pack we already found
        try:
            m = loads_lenient(zf.read(info))
        except Exception as e:
            warnings.append(f"{source}: couldn't read {prefix}manifest.json ({e})")
            continue
        if not isinstance(m, dict) or "header" not in m:
            continue
        roots.append(prefix)
        packs.append(Pack(zf, prefix, m, source))

    # .mcaddons frequently contain .mcpack files (which are zips themselves)
    if depth < 3:
        for info in zf.infolist():
            n = info.filename.replace("\\", "/")
            if n.lower().endswith((".mcpack", ".mcaddon", ".zip")):
                label = f"{source} › {os.path.basename(n)}"
                try:
                    inner = zipfile.ZipFile(io.BytesIO(zf.read(info)))
                except zipfile.BadZipFile:
                    warnings.append(f"{label}: not a valid archive, skipped")
                    continue
                p, w = scan_archive(inner, label, depth + 1)
                packs += p
                warnings += w
    return packs, warnings


# --------------------------------------------------------------------------- #
#  Packs installed on the server
# --------------------------------------------------------------------------- #
class InstalledPack:
    def __init__(self, kind, packs_dir, folder, manifest):
        h = manifest.get("header", {}) or {}
        self.kind = kind
        self.packs_dir = packs_dir
        self.folder = folder
        self.path = os.path.join(packs_dir, folder)
        self.uuid_raw = str(h.get("uuid", ""))
        self.uuid = self.uuid_raw.lower()
        self.version = h.get("version")
        self.deps = manifest_dep_uuids(manifest)
        lang = None
        lp = os.path.join(self.path, "texts", "en_US.lang")
        if os.path.isfile(lp):
            try:
                with open(lp, "rb") as f:
                    lang = f.read()
            except OSError:
                pass
        self.name = resolve_pack_name(str(h.get("name", "") or ""), lang, folder)
        self.in_world = None  # True / False / None (no world set)


def list_installed(packs_dir, kind):
    out = []
    if not packs_dir or not os.path.isdir(packs_dir):
        return out
    for entry in sorted(os.scandir(packs_dir), key=lambda e: e.name.lower()):
        if not entry.is_dir() or entry.name.startswith(".packdeploy_tmp_"):
            continue
        mp = os.path.join(entry.path, "manifest.json")
        if not os.path.isfile(mp):
            continue
        try:
            m = read_json_file(mp, {})
            if isinstance(m, dict) and (m.get("header") or {}).get("uuid"):
                out.append(InstalledPack(kind, packs_dir, entry.name, m))
        except Exception:
            pass
    return out


def index_installed(packs_dir, kind=None):
    """uuid -> (folder name, version) for packs already in a packs folder."""
    return {p.uuid: (p.folder, p.version) for p in list_installed(packs_dir, kind)}


# --------------------------------------------------------------------------- #
#  World pack lists
# --------------------------------------------------------------------------- #
def load_world_list(world_dir, kind):
    path = os.path.join(world_dir, WORLD_FILES[kind])
    data = read_json_file(path, [])
    if not isinstance(data, list):
        raise ValueError(f"{WORLD_FILES[kind]} isn't a list")
    return path, data


def save_world_list(path, data, log):
    if os.path.exists(path):
        backup_file(path, log)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        log(f"✓ Updated {os.path.basename(path)}", "good")
        return True
    except OSError as e:
        log(f"✗ Couldn't write {os.path.basename(path)} ({e}). Check that your Windows user "
            f"can write to this folder / that the file isn't read-only.", "bad")
        return False


def world_uuids(world_dir):
    out = {}
    for kind in WORLD_FILES:
        try:
            _, data = load_world_list(world_dir, kind)
            out[kind] = {str(e.get("pack_id", "")).lower() for e in data if isinstance(e, dict)}
        except Exception:
            out[kind] = set()
    return out


def update_world(world_dir, done, add_new, log):
    for kind, fname in WORLD_FILES.items():
        mine = [p for p in done if p.kind == kind]
        if not mine:
            continue
        try:
            path, data = load_world_list(world_dir, kind)
        except Exception as e:
            log(f"✗ Couldn't read {fname} ({e}) — left untouched", "bad")
            continue

        changed = False
        for p in mine:
            entry = next((e for e in data if isinstance(e, dict)
                          and str(e.get("pack_id", "")).lower() == p.uuid), None)
            if entry is not None:
                if entry.get("version") != p.version:
                    log(f"  {fname}: {p.name} version {ver_str(entry.get('version'))} → {ver_str(p.version)}")
                    entry["version"] = p.version
                    changed = True
            elif add_new:
                data.append({"pack_id": p.uuid_raw, "version": p.version})
                log(f"  {fname}: added {p.name}")
                changed = True
            else:
                log(f"  {fname}: {p.name} not enabled on this world (add-new is off)", "warn")

        if changed:
            save_world_list(path, data, log)
        else:
            log(f"  {fname} already up to date", "dim")


def remove_from_world(world_dir, packs, log):
    for kind, fname in WORLD_FILES.items():
        mine = {p.uuid: p for p in packs if p.kind == kind}
        if not mine:
            continue
        try:
            path, data = load_world_list(world_dir, kind)
        except Exception as e:
            log(f"✗ Couldn't read {fname} ({e}) — left untouched", "bad")
            continue
        keep = []
        for e in data:
            u = str(e.get("pack_id", "")).lower() if isinstance(e, dict) else ""
            if u in mine:
                log(f"  {fname}: removed {mine[u].name}")
            else:
                keep.append(e)
        if len(keep) != len(data):
            save_world_list(path, keep, log)
        else:
            log(f"  {fname}: nothing to remove", "dim")


# --------------------------------------------------------------------------- #
#  Install / remove
# --------------------------------------------------------------------------- #
def plan(packs, rp_dir, bp_dir):
    dirs = {"resource": rp_dir, "behavior": bp_dir}
    idx = {k: index_installed(d) for k, d in dirs.items()}
    taken = {}
    for k, d in dirs.items():
        taken[k] = {e.lower() for e in os.listdir(d)} if d and os.path.isdir(d) else set()

    for p in packs:
        p.target_dir = p.target_folder = p.old_version = None
        if p.kind not in DEPLOYABLE:
            p.status = f"{KIND_LABEL[p.kind]} — not deployable to a server"
            continue
        d = dirs[p.kind]
        if not d:
            p.status = f"Choose a {KIND_LABEL[p.kind].lower()} packs folder"
            continue
        p.target_dir = d
        hit = idx[p.kind].get(p.uuid)
        if hit:
            p.target_folder, p.old_version = hit
            if ver_str(hit[1]) == ver_str(p.version):
                p.status = f"Reinstall over {hit[0]}  (same version {ver_str(p.version)})"
            else:
                p.status = f"Update {hit[0]}  ({ver_str(hit[1])} → {ver_str(p.version)})"
        else:
            base = safe_folder_name(p.name, p.uuid)
            name, k = base, 2
            while name.lower() in taken[p.kind]:
                name = f"{base}_{k}"
                k += 1
            taken[p.kind].add(name.lower())
            p.target_folder = name
            p.status = f"New → {name}"


def install_pack(p, backup, log):
    d = p.target_dir
    final = os.path.join(d, p.target_folder)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".packdeploy_tmp_{uuidlib.uuid4().hex[:8]}")
    os.makedirs(tmp)
    tmp_abs = os.path.abspath(tmp)
    try:
        count = 0
        for info, rel in p.members():
            dest = os.path.abspath(os.path.join(tmp, *rel.split("/")))
            if not dest.startswith(tmp_abs + os.sep):
                log(f"  skipped unsafe path in archive: {rel}", "warn")
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with p.zf.open(info) as src, open(dest, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1

        if os.path.exists(final):
            if backup:
                bdir = backup_root_for(d)
                os.makedirs(bdir, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                bpath = os.path.join(bdir, f"{p.target_folder}_v{ver_str(p.old_version)}_{stamp}")
                shutil.move(final, bpath)
                log(f"  old version backed up to {bpath}", "dim")
            else:
                rmtree(final)
        os.rename(tmp, final)
        return count
    except Exception:
        rmtree(tmp)
        raise


def remove_installed(ip, backup, log):
    if backup:
        bdir = backup_root_for(ip.packs_dir)
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        bpath = os.path.join(bdir, f"{ip.folder}_v{ver_str(ip.version)}_{stamp}_removed")
        shutil.move(ip.path, bpath)
        log(f"✓ Removed {ip.name} (moved to {bpath})", "good")
    else:
        rmtree(ip.path)
        log(f"✓ Deleted {ip.name} ({ip.path})", "good")


# --------------------------------------------------------------------------- #
#  PackDoctor
# --------------------------------------------------------------------------- #
class Target:
    """A pack PackDoctor can read (and fix): either inside an archive or a folder."""

    def __init__(self, name, kind, uuid, version, label, pack=None, path=None):
        self.name, self.kind, self.uuid, self.version, self.label = name, kind, uuid, version, label
        self.pack, self.path = pack, path
        self._zip_index = {rel: info for info, rel in pack.members()} if pack else None

    @classmethod
    def from_pack(cls, p):
        return cls(p.name, p.kind, p.uuid, p.version, p.source, pack=p)

    @classmethod
    def from_installed(cls, ip):
        return cls(ip.name, ip.kind, ip.uuid, ip.version, ip.path, path=ip.path)

    @property
    def in_place(self):
        return self.path is not None

    def list_files(self):
        if self._zip_index is not None:
            return sorted(self._zip_index)
        out = []
        for root, _dirs, files in os.walk(self.path):
            for f in files:
                full = os.path.join(root, f)
                out.append(os.path.relpath(full, self.path).replace(os.sep, "/"))
        return sorted(out)

    def read(self, rel):
        if self._zip_index is not None:
            return self.pack.zf.read(self._zip_index[rel])
        with open(os.path.join(self.path, *rel.split("/")), "rb") as f:
            return f.read()


class Issue:
    __slots__ = ("target", "file", "sev", "msg", "detail", "auto")

    def __init__(self, target, file, sev, msg, detail, auto):
        self.target, self.file, self.sev, self.msg, self.detail, self.auto = \
            target, file, sev, msg, detail, auto


SEV_ORDER = {"Error": 0, "Warning": 1, "Fixable": 2, "Info": 3}

HCF_DETAIL = (
    "This uses Holiday Creator Features (the old experimental toggle) — event triggers such "
    "as minecraft:on_interact / on_placed / queued_ticking plus an \"events\" section. Mojang "
    "removed these, so the block or item either fails to load or stops responding (chairs you "
    "can't sit on, drawers that won't open, etc.).\n\n"
    "This can't be converted automatically: the logic has to be rewritten as Custom Components "
    "with a small Script API (JavaScript) file. It's mechanical work — each trigger maps to a "
    "custom-component event (onPlayerInteract, onPlace, onTick, ...)."
)
OLD_SCRIPT_DETAIL = (
    "This pack uses the original pre-release scripting API module names (mojang-minecraft, "
    "mojang-gametest, ...). Those were renamed to @minecraft/server etc. and the old names no "
    "longer load. The scripts need updating to the current API before the pack will work."
)
BETA_DETAIL = (
    "This pack depends on a BETA version of the Script API. Beta versions are removed or "
    "changed with almost every Minecraft update, so scripts that worked before now fail to "
    "load — a very common reason packs stop working.\n\n"
    "Fix: change the version in manifest.json to a stable release supported by your server "
    "version (see Mojang's Script API docs), then check the content log. If the scripts used "
    "beta-only functions, those calls also need updating. With the Beta APIs experiment on, "
    "the beta version must exactly match the one your server version ships with."
)
RENAME_DETAIL = (
    "This component was renamed or restructured in a newer format. Auto-fix converts it to the "
    "current name and format and raises the file's format_version so Minecraft reads it the new way."
)
REMOVED_DETAIL = (
    "This component no longer exists and is ignored or causes a load error. Auto-fix removes it."
)
EXPERIMENTAL_DETAIL = (
    "\"is_experimental\": true makes this only load when an experimental toggle is on. "
    "Auto-fix removes the flag so it loads normally."
)
UUID_DETAIL = (
    "A module UUID is the same as the pack's header UUID (or another module's). Minecraft "
    "rejects packs with duplicate UUIDs. Auto-fix gives the module a fresh UUID — the pack's "
    "own UUID is untouched, so world lists and dependencies still match."
)
STRVER_DETAIL = (
    "With manifest format_version 1/2 the version must be written as [major, minor, patch], "
    "not as text. Auto-fix converts it."
)
INVALID_JSON_DETAIL = (
    "Minecraft couldn't read this file, even allowing comments and trailing commas, so whatever "
    "it defines (a block, item, entity, texture list...) is skipped. Open it in a text editor and "
    "fix the syntax error shown."
)
MISSING_DEP_DETAIL = (
    "The manifest says this pack needs another pack (usually its resource/behavior other half), "
    "but that pack isn't installed or loaded. Minecraft may refuse to load this pack without it."
)
OLD_ENGINE_DETAIL = (
    "min_engine_version tells Minecraft which version the pack was made for. Old values make "
    "Minecraft run some files in legacy-compatibility mode. Not an error on its own, but a hint "
    "that the pack is old if things look off. It isn't auto-changed because raising it can change "
    "how the whole pack is interpreted."
)
PART_VIS_DETAIL = (
    "minecraft:part_visibility was replaced by bone_visibility inside minecraft:geometry. It needs "
    "converting by hand because the conditions have to move into the geometry component."
)
CREATIVE_ITEM_DETAIL = (
    "minecraft:creative_category was replaced by \"menu_category\" in the item's description. "
    "Move it there (category / group) and bump the item's format_version."
)

OLD_SCRIPT_NAMES = {"mojang-minecraft", "mojang-gametest", "mojang-minecraft-ui",
                    "mojang-minecraft-server-admin", "mojang-net", "mojang-minecraft-server-net"}

HCF_BLOCK_COMPONENTS = {
    "minecraft:on_interact", "minecraft:on_placed", "minecraft:on_player_placing",
    "minecraft:on_step_on", "minecraft:on_step_off", "minecraft:on_fall_on",
    "minecraft:on_player_destroyed", "minecraft:queued_ticking", "minecraft:random_ticking",
    "minecraft:ticking",
}
HCF_ITEM_COMPONENTS = {"minecraft:on_use", "minecraft:on_use_on", "minecraft:weapon",
                       "minecraft:chargeable"}
REMOVED_BLOCK_COMPONENTS = {"minecraft:breakonpush", "minecraft:immovable",
                            "minecraft:onlypistonpush", "minecraft:preventsjumping",
                            "minecraft:unwalkable"}
BLOCK_TARGET_FORMAT = "1.21.40"


def has_event_trigger(obj):
    """HCF event triggers look like {"event": "...", "target": "self"}."""
    if isinstance(obj, dict):
        if isinstance(obj.get("event"), str):
            return True
        return any(has_event_trigger(v) for v in obj.values())
    if isinstance(obj, list):
        return any(has_event_trigger(v) for v in obj)
    return False


def replace_strings(obj, pairs):
    changed = False
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str):
                nv = v
                for a, b in pairs:
                    nv = nv.replace(a, b)
                if nv != v:
                    obj[k] = nv
                    changed = True
            elif replace_strings(v, pairs):
                changed = True
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                nv = v
                for a, b in pairs:
                    nv = nv.replace(a, b)
                if nv != v:
                    obj[i] = nv
                    changed = True
            elif replace_strings(v, pairs):
                changed = True
    return changed


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def fix_block_components(comps, block, notes):
    def note(text, detail=RENAME_DETAIL):
        if text not in [n[0] for n in notes]:
            notes.append((text, detail))

    # light emission: old 0.0–1.0 float  -> new 0–15 int
    if "minecraft:block_light_emission" in comps:
        v = comps.pop("minecraft:block_light_emission")
        if "minecraft:light_emission" not in comps:
            comps["minecraft:light_emission"] = int(round(v * 15)) if _num(v) and v <= 1 else \
                (max(0, min(15, int(round(v)))) if _num(v) else 0)
        note("minecraft:block_light_emission → minecraft:light_emission")
    # light absorption: old names -> light_dampening (0–15 int)
    for old in ("minecraft:block_light_absorption", "minecraft:block_light_filter"):
        if old in comps:
            v = comps.pop(old)
            if "minecraft:light_dampening" not in comps:
                comps["minecraft:light_dampening"] = max(0, min(15, int(round(v)))) if _num(v) else 15
            note(f"{old} → minecraft:light_dampening")
    for old, new in (("minecraft:entity_collision", "minecraft:collision_box"),
                     ("minecraft:pick_collision", "minecraft:selection_box"),
                     ("minecraft:aim_collision", "minecraft:selection_box")):
        if old in comps:
            v = comps.pop(old)
            comps.setdefault(new, v)
            note(f"{old} → {new}")
    if "minecraft:destroy_time" in comps:
        v = comps.pop("minecraft:destroy_time")
        if "minecraft:destructible_by_mining" not in comps:
            comps["minecraft:destructible_by_mining"] = (
                {"seconds_to_destroy": v} if _num(v) else v)
        note("minecraft:destroy_time → minecraft:destructible_by_mining")
    if "minecraft:explosion_resistance" in comps:
        v = comps.pop("minecraft:explosion_resistance")
        if "minecraft:destructible_by_explosion" not in comps:
            comps["minecraft:destructible_by_explosion"] = (
                {"explosion_resistance": v} if _num(v) else v)
        note("minecraft:explosion_resistance → minecraft:destructible_by_explosion")
    if "minecraft:unit_cube" in comps:
        comps.pop("minecraft:unit_cube")
        comps.setdefault("minecraft:geometry", "minecraft:geometry.full_block")
        note("minecraft:unit_cube → minecraft:geometry.full_block")
    if "minecraft:rotation" in comps:
        v = comps.pop("minecraft:rotation")
        if "minecraft:transformation" not in comps and isinstance(v, list):
            comps["minecraft:transformation"] = {"rotation": v}
        note("minecraft:rotation → minecraft:transformation")
    if "minecraft:creative_category" in comps:
        v = comps.pop("minecraft:creative_category")
        desc = block.setdefault("description", {})
        if isinstance(v, dict) and "menu_category" not in desc:
            mc = {"category": v.get("category", "construction")}
            if v.get("group"):
                mc["group"] = v["group"]
            desc["menu_category"] = mc
        note("minecraft:creative_category → description.menu_category")
    for k in REMOVED_BLOCK_COMPONENTS & set(comps):
        comps.pop(k)
        note(f"Removed obsolete {k}", REMOVED_DETAIL)


def check_block(data, rel, add):
    b = data.get("minecraft:block")
    if not isinstance(b, dict):
        return False
    notes, hcf = [], set()
    if "events" in b:
        hcf.add('"events"')
    comp_sets = [b.get("components")]
    for p in b.get("permutations", []) or []:
        if isinstance(p, dict):
            comp_sets.append(p.get("components"))
    for comps in comp_sets:
        if not isinstance(comps, dict):
            continue
        hcf |= HCF_BLOCK_COMPONENTS & set(comps)
        if "minecraft:part_visibility" in comps:
            add(rel, "Warning", "minecraft:part_visibility must be converted to bone_visibility",
                PART_VIS_DETAIL)
        fix_block_components(comps, b, notes)

    desc = b.get("description")
    if isinstance(desc, dict):
        if "properties" in desc and "states" not in desc:
            desc["states"] = desc.pop("properties")
            notes.append(("description.properties → description.states", RENAME_DETAIL))
        if desc.pop("is_experimental", None) is not None:
            notes.append(("Removed is_experimental flag", EXPERIMENTAL_DETAIL))
        if "register_to_creative_menu" in desc:
            v = desc.pop("register_to_creative_menu")
            if v and "menu_category" not in desc:
                desc["menu_category"] = {"category": "construction"}
            notes.append(("register_to_creative_menu → menu_category", RENAME_DETAIL))
    if replace_strings(b, [("query.block_property", "query.block_state"),
                           ("q.block_property", "q.block_state"),
                           ("query.has_block_property", "query.has_block_state"),
                           ("q.has_block_property", "q.has_block_state")]):
        notes.append(("query.block_property → query.block_state (Molang)", RENAME_DETAIL))

    if hcf:
        shown = sorted(hcf)
        more = f" +{len(shown) - 3} more" if len(shown) > 3 else ""
        add(rel, "Error", "Uses removed Holiday Creator Features: " + ", ".join(shown[:3]) + more,
            HCF_DETAIL)
    if notes:
        fv = data.get("format_version")
        if vtuple(fv) < vtuple(BLOCK_TARGET_FORMAT):
            data["format_version"] = BLOCK_TARGET_FORMAT
            notes.append((f"format_version {fv} → {BLOCK_TARGET_FORMAT}", RENAME_DETAIL))
        for text, detail in notes:
            add(rel, "Fixable", text, detail, True)
    return bool(notes)


def check_item(data, rel, add):
    it = data.get("minecraft:item")
    if not isinstance(it, dict) or vtuple(data.get("format_version")) < (1, 16, 100):
        return False  # legacy item format: still supported
    changed = False
    comps = it.get("components") if isinstance(it.get("components"), dict) else {}
    hcf = HCF_ITEM_COMPONENTS & set(comps)
    if "events" in it:
        hcf.add('"events"')
    if any(has_event_trigger(v) for v in comps.values()):
        hcf.add("event triggers")
    if hcf:
        add(rel, "Error", "Uses removed Holiday Creator Features: " + ", ".join(sorted(hcf)[:3]),
            HCF_DETAIL)
    if "minecraft:creative_category" in comps:
        add(rel, "Warning", "minecraft:creative_category was replaced by menu_category",
            CREATIVE_ITEM_DETAIL)
    desc = it.get("description")
    if isinstance(desc, dict) and desc.pop("is_experimental", None) is not None:
        add(rel, "Fixable", "Removed is_experimental flag", EXPERIMENTAL_DETAIL, True)
        changed = True
    return changed


def check_entity(data, rel, add):
    ent = data.get("minecraft:entity")
    desc = ent.get("description") if isinstance(ent, dict) else None
    if isinstance(desc, dict) and desc.get("is_experimental"):
        desc.pop("is_experimental")
        add(rel, "Fixable", "Removed is_experimental flag", EXPERIMENTAL_DETAIL, True)
        return True
    return False


def check_manifest(m, rel, add, known, t):
    changed = False
    h = m.get("header") if isinstance(m.get("header"), dict) else {}
    fv = m.get("format_version")
    hu = str(h.get("uuid", "")).lower()
    if not is_uuid(hu):
        add(rel, "Error", "Pack UUID is missing or invalid",
            "header.uuid must be a valid UUID or Minecraft won't load the pack.")
    if isinstance(h.get("version"), str) and str(fv) in ("1", "2"):
        sv = parse_semver(h["version"])
        if sv:
            h["version"] = sv
            add(rel, "Fixable", "Header version written as text → [x, y, z]", STRVER_DETAIL, True)
            changed = True

    seen = {hu}
    for mod in m.get("modules", []) or []:
        if not isinstance(mod, dict):
            continue
        mu = str(mod.get("uuid", "")).lower()
        if mu in seen or not is_uuid(mu):
            mod["uuid"] = str(uuidlib.uuid4())
            add(rel, "Fixable", f"Module UUID duplicated/invalid ({mod.get('type', '?')}) → new UUID",
                UUID_DETAIL, True)
            changed = True
        seen.add(str(mod.get("uuid")).lower())
        if isinstance(mod.get("version"), str) and str(fv) in ("1", "2"):
            sv = parse_semver(mod["version"])
            if sv:
                mod["version"] = sv
                add(rel, "Fixable", "Module version written as text → [x, y, z]", STRVER_DETAIL, True)
                changed = True

    mev = h.get("min_engine_version")
    if t.kind in DEPLOYABLE:
        if mev is None:
            add(rel, "Info", "No min_engine_version (pack is probably very old)", OLD_ENGINE_DETAIL)
        elif vtuple(mev) < (1, 20, 0):
            add(rel, "Info", f"Made for Minecraft {ver_str(mev)}", OLD_ENGINE_DETAIL)

    for d in m.get("dependencies", []) or []:
        if not isinstance(d, dict):
            continue
        name = d.get("module_name")
        if name:
            ver = d.get("version")
            if name in OLD_SCRIPT_NAMES:
                add(rel, "Error", f"Uses the old '{name}' script API (renamed long ago)", OLD_SCRIPT_DETAIL)
            elif "beta" in ver_str(ver).lower():
                add(rel, "Warning", f"Depends on beta script API {name} {ver_str(ver)}", BETA_DETAIL)
        elif d.get("uuid"):
            du = str(d["uuid"]).lower()
            if du not in known:
                add(rel, "Warning", f"Needs a pack that isn't installed or loaded ({du[:8]}…)",
                    MISSING_DEP_DETAIL)
    return changed


def doctor_scan(t, known_uuids):
    """Returns (issues, fixes) where fixes = {rel: new bytes}."""
    issues, fixes = [], {}

    def add(rel, sev, msg, detail, auto=False):
        issues.append(Issue(t, rel, sev, msg, detail, auto))

    for rel in t.list_files():
        if not rel.lower().endswith(".json"):
            continue
        try:
            raw = t.read(rel)
        except OSError as e:
            add(rel, "Warning", f"Couldn't read file ({e})", "")
            continue
        try:
            data = loads_lenient(raw)
        except Exception as e:
            add(rel, "Error", "Invalid JSON — Minecraft will skip this file", f"{e}\n\n{INVALID_JSON_DETAIL}")
            continue
        if not isinstance(data, dict):
            continue
        new = copy.deepcopy(data)
        if rel.lower() == "manifest.json":
            changed = check_manifest(new, rel, add, known_uuids, t)
        elif t.kind == "behavior" and "minecraft:block" in new:
            changed = check_block(new, rel, add)
        elif t.kind == "behavior" and "minecraft:item" in new:
            changed = check_item(new, rel, add)
        elif t.kind == "behavior" and "minecraft:entity" in new:
            changed = check_entity(new, rel, add)
        else:
            changed = False
        if changed:
            fixes[rel] = (json.dumps(new, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    return issues, fixes


def export_fixed(targets, fixes, out_path):
    """Write every target into one .mcaddon, with fixed files swapped in."""
    used = set()
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for t in targets:
            suffix = "RP" if t.kind == "resource" else "BP"
            label = t.name if t.name.upper().endswith(suffix) else f"{t.name} {suffix}"
            base = safe_folder_name(label, t.uuid)
            folder, k = base, 2
            while folder.lower() in used:
                folder = f"{base}_{k}"
                k += 1
            used.add(folder.lower())
            mine = fixes.get(id(t), {})
            for rel in t.list_files():
                z.writestr(f"{folder}/{rel}", mine.get(rel) if rel in mine else t.read(rel))


def apply_fixes_in_place(t, fixes, log):
    bdir = backup_root_for(os.path.dirname(t.path))
    os.makedirs(bdir, exist_ok=True)
    bpath = os.path.join(bdir, f"{os.path.basename(t.path)}_v{ver_str(t.version)}_"
                               f"{time.strftime('%Y%m%d-%H%M%S')}_before_fix")
    shutil.copytree(t.path, bpath, copy_function=shutil.copyfile)
    log(f"  backed up {t.name} to {bpath}", "dim")
    for rel, data in fixes.items():
        dest = os.path.join(t.path, *rel.split("/"))
        try:
            os.chmod(dest, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        with open(dest, "wb") as f:
            f.write(data)
    log(f"✓ Fixed {len(fixes)} file(s) in {t.name}", "good")


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} {APP_VERSION}")
        self.geometry("1040x740")
        self.minsize(860, 600)
        self.configure(bg=BG)
        self._set_icon()

        self.cfg = self._load_cfg()
        self.packs = []          # loaded add-on packs (Deploy tab)
        self.archives = []
        self.installed = []      # InstalledPack list (Installed tab)
        self.doc_targets = []    # PackDoctor
        self.doc_issues = []
        self.doc_fixes = {}
        self.q = queue.Queue()
        self.busy = False
        self._refresh_job = None

        self.var_addon = tk.StringVar(value="No add-on loaded")
        self.var_rp = tk.StringVar(value=self.cfg.get("rp", ""))
        self.var_bp = tk.StringVar(value=self.cfg.get("bp", ""))
        self.var_world = tk.StringVar(value=self.cfg.get("world", ""))
        self.var_backup = tk.BooleanVar(value=self.cfg.get("backup", True))
        self.var_addnew = tk.BooleanVar(value=self.cfg.get("add_new", True))
        self.var_doc_src = tk.StringVar(value="Nothing scanned yet")

        self._style()
        self._build()
        for v in (self.var_rp, self.var_bp, self.var_world):
            v.trace_add("write", lambda *_: self._schedule_refresh())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
        self.refresh_all()
        self.log("Load a .mcaddon / .mcpack, check the folders, then hit Deploy.", "dim")

    def _set_icon(self):
        # PyInstaller --onefile unpacks bundled data to sys._MEIPASS
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base, "assets", "icon.ico")
        if os.path.isfile(path):
            try:
                self.iconbitmap(default=path)
            except tk.TclError:
                pass

    # ---- config ----
    def _load_cfg(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_cfg(self):
        self.cfg.update(
            rp=self.var_rp.get().strip(),
            bp=self.var_bp.get().strip(),
            world=self.var_world.get().strip(),
            backup=self.var_backup.get(),
            add_new=self.var_addnew.get(),
        )
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)
        except OSError:
            pass

    def _on_close(self):
        self._save_cfg()
        self.destroy()

    # ---- look ----
    def _style(self):
        s = ttk.Style(self)
        s.theme_use("clam")
        s.configure("Treeview", background=FIELD, fieldbackground=FIELD, foreground=FG,
                    bordercolor=BORDER, rowheight=26, font=("Segoe UI", 10))
        s.configure("Treeview.Heading", background=PANEL, foreground=FG, relief="flat",
                    font=("Segoe UI", 10, "bold"))
        s.map("Treeview.Heading", background=[("active", BORDER)])
        s.map("Treeview", background=[("selected", "#264f78")], foreground=[("selected", FG)])
        s.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG,
                    bordercolor=BG, arrowcolor=FG)
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 4, 0, 0))
        s.configure("TNotebook.Tab", background=PANEL, foreground=DIM, padding=(16, 6),
                    borderwidth=0, font=("Segoe UI", 10))
        s.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", FG)])
        s.configure("TFrame", background=BG)

    def _btn(self, parent, text, cmd, accent=False, danger=False):
        bg = ACCENT if accent else ("#8a3a3a" if danger else "#3a3a3d")
        abg = "#2f66b3" if accent else ("#a04545" if danger else "#4a4a4e")
        return tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, cursor="hand2",
                         bg=bg, fg="white" if (accent or danger) else FG,
                         activebackground=abg, activeforeground="white", padx=14, pady=5,
                         font=("Segoe UI", 10, "bold" if accent else "normal"))

    def _check(self, parent, text, var):
        return tk.Checkbutton(parent, text=text, variable=var, bg=BG, fg=FG, selectcolor=FIELD,
                              activebackground=BG, activeforeground=FG, font=("Segoe UI", 10),
                              highlightthickness=0, bd=0)

    def _entry(self, parent, var, readonly=False):
        e = tk.Entry(parent, textvariable=var, bg=FIELD, fg=FG, insertbackground=FG, relief="flat",
                     highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                     font=("Segoe UI", 10), readonlybackground=FIELD)
        if readonly:
            e.config(state="readonly")
        return e

    def _tree(self, parent, cols):
        frame = tk.Frame(parent, bg=BG)
        tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings", selectmode="extended")
        for key, text, width, stretch, anchor in cols:
            tree.heading(key, text=text)
            tree.column(key, width=width, stretch=stretch, anchor=anchor)
        for tag, color in (("new", GOOD), ("update", INFO), ("off", DIM), ("warn", WARN),
                           ("bad", BAD), ("good", GOOD), ("info", DIM), ("fix", INFO)):
            tree.tag_configure(tag, foreground=color)
        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        return frame, tree

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=14, pady=(14, 4))
        top.columnconfigure(1, weight=1)
        rows = [
            ("Resource packs folder", self.var_rp,
             lambda: self.pick_dir(self.var_rp, "Select the server's resource_packs folder")),
            ("Behavior packs folder", self.var_bp,
             lambda: self.pick_dir(self.var_bp, "Select the server's behavior_packs folder")),
            ("World folder (optional)", self.var_world,
             lambda: self.pick_dir(self.var_world, "Select the world folder (worlds\\<name>)")),
        ]
        for r, (label, var, cmd) in enumerate(rows):
            tk.Label(top, text=label, bg=BG, fg=FG, font=("Segoe UI", 10), anchor="w") \
                .grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            self._entry(top, var).grid(row=r, column=1, sticky="ew", ipady=4)
            self._btn(top, "Browse…", cmd).grid(row=r, column=2, padx=(8, 0), sticky="ew")

        opts = tk.Frame(self, bg=BG)
        opts.pack(fill="x", padx=14, pady=(4, 6))
        self._btn(opts, "Auto-fill from server folder…", self.pick_server).pack(side="left")
        self._check(opts, "Back up replaced / removed packs", self.var_backup).pack(side="left", padx=(16, 0))

        # log is packed before the notebook (at the bottom) so it never gets squeezed out
        self.logbox = tk.Text(self, height=7, bg=PANEL, fg=FG, relief="flat", wrap="word",
                              font=("Consolas", 9), highlightthickness=0, padx=8, pady=6)
        self.logbox.pack(side="bottom", fill="x", padx=14, pady=(8, 14))

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True, padx=14)
        self._build_deploy(ttk.Frame(self.nb))
        self._build_installed(ttk.Frame(self.nb))
        self._build_doctor(ttk.Frame(self.nb))
        for tag, color in (("good", GOOD), ("bad", BAD), ("warn", WARN), ("dim", DIM)):
            self.logbox.tag_configure(tag, foreground=color)
        self.logbox.config(state="disabled")

    # ---- Deploy tab ----
    def _build_deploy(self, tab):
        self.nb.add(tab, text="  Deploy  ")
        row = tk.Frame(tab, bg=BG)
        row.pack(fill="x", pady=(10, 6))
        row.columnconfigure(1, weight=1)
        tk.Label(row, text="Add-on file(s)", bg=BG, fg=FG, font=("Segoe UI", 10)) \
            .grid(row=0, column=0, sticky="w", padx=(0, 10))
        self._entry(row, self.var_addon, readonly=True).grid(row=0, column=1, sticky="ew", ipady=4)
        self._btn(row, "Browse…", self.pick_addon).grid(row=0, column=2, padx=(8, 0))

        frame, self.tree = self._tree(tab, [
            ("use", "✓", 40, False, "center"), ("name", "Pack", 240, True, "w"),
            ("type", "Type", 90, False, "w"), ("version", "Version", 80, False, "center"),
            ("action", "What will happen", 360, True, "w"),
        ])
        frame.pack(fill="both", expand=True)
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.selection()))

        act = tk.Frame(tab, bg=BG)
        act.pack(fill="x", pady=8)
        self.deploy_btn = self._btn(act, "Deploy to server", self.deploy, accent=True)
        self.deploy_btn.pack(side="left")
        self._check(act, "Add new packs to world", self.var_addnew).pack(side="left", padx=(12, 0))
        self._btn(act, "Check with PackDoctor", self.doctor_loaded).pack(side="left", padx=(12, 0))
        tk.Label(act, text="Click ✓ or press Space to include/exclude", bg=BG, fg=DIM,
                 font=("Segoe UI", 9)).pack(side="right")

    # ---- Installed tab ----
    def _build_installed(self, tab):
        self.nb.add(tab, text="  Installed  ")
        tk.Label(tab, text="Packs currently in your server's pack folders. Select one or more (Ctrl/Shift-click).",
                 bg=BG, fg=DIM, font=("Segoe UI", 9), anchor="w").pack(fill="x", pady=(10, 4))
        frame, self.itree = self._tree(tab, [
            ("name", "Pack", 240, True, "w"), ("type", "Type", 90, False, "w"),
            ("version", "Version", 80, False, "center"), ("folder", "Folder", 220, True, "w"),
            ("world", "In world", 80, False, "center"),
        ])
        frame.pack(fill="both", expand=True)

        act = tk.Frame(tab, bg=BG)
        act.pack(fill="x", pady=8)
        self._btn(act, "Remove selected…", self.remove_selected, danger=True).pack(side="left")
        self._btn(act, "Enable in world", lambda: self.set_in_world(True)).pack(side="left", padx=(8, 0))
        self._btn(act, "Disable in world", lambda: self.set_in_world(False)).pack(side="left", padx=(8, 0))
        self._btn(act, "Check with PackDoctor", self.doctor_installed).pack(side="left", padx=(8, 0))
        self._btn(act, "Open folder", self.open_selected).pack(side="left", padx=(8, 0))
        self._btn(act, "Refresh", self.refresh_installed).pack(side="right")

    # ---- PackDoctor tab ----
    def _build_doctor(self, tab):
        self.nb.add(tab, text="  PackDoctor  ")
        src = tk.Frame(tab, bg=BG)
        src.pack(fill="x", pady=(10, 6))
        self._btn(src, "Scan loaded add-on", self.doctor_loaded).pack(side="left")
        self._btn(src, "Scan selected installed", self.doctor_installed).pack(side="left", padx=(8, 0))
        self._btn(src, "Scan a file…", self.doctor_file).pack(side="left", padx=(8, 0))
        tk.Label(src, textvariable=self.var_doc_src, bg=BG, fg=DIM, font=("Segoe UI", 9)) \
            .pack(side="left", padx=(14, 0))

        pane = tk.PanedWindow(tab, orient="vertical", bg=BG, sashwidth=6, bd=0)
        pane.pack(fill="both", expand=True)
        frame, self.dtree = self._tree(pane, [
            ("sev", "Severity", 80, False, "w"), ("pack", "Pack", 170, False, "w"),
            ("file", "File", 220, False, "w"), ("issue", "Issue", 420, True, "w"),
        ])
        pane.add(frame, minsize=120, stretch="always")
        self.dtree.bind("<<TreeviewSelect>>", self._show_issue)
        self.ddetail = tk.Text(pane, height=6, bg=PANEL, fg=FG, relief="flat", wrap="word",
                               font=("Segoe UI", 10), highlightthickness=0, padx=10, pady=8)
        self.ddetail.config(state="disabled")
        pane.add(self.ddetail, minsize=60)

        act = tk.Frame(tab, bg=BG)
        act.pack(fill="x", pady=8)
        self.export_btn = self._btn(act, "Export fixed copy…", self.doctor_export, accent=True)
        self.export_btn.pack(side="left")
        self.inplace_btn = self._btn(act, "Fix installed pack(s) in place", self.doctor_inplace)
        self.inplace_btn.pack(side="left", padx=(8, 0))
        self.var_doc_sum = tk.StringVar(value="")
        tk.Label(act, textvariable=self.var_doc_sum, bg=BG, fg=DIM, font=("Segoe UI", 9)) \
            .pack(side="right")

    # ---- helpers ----
    def log(self, msg, tag=""):
        self.logbox.config(state="normal")
        self.logbox.insert("end", msg + "\n", tag)
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def _open(self, path):
        path = path.strip()
        if path and os.path.isdir(path):
            os.startfile(path)
        else:
            messagebox.showinfo(APP, "That folder isn't set or doesn't exist.")

    def pick_dir(self, var, title):
        start = var.get().strip() or self.cfg.get("last_dir", "")
        d = filedialog.askdirectory(title=title, initialdir=start if os.path.isdir(start) else None)
        if d:
            var.set(os.path.normpath(d))
            self.cfg["last_dir"] = os.path.dirname(os.path.normpath(d))
            self._save_cfg()

    def pick_server(self):
        d = filedialog.askdirectory(title="Select your Bedrock server folder")
        if not d:
            return
        found = []
        for sub, var in (("resource_packs", self.var_rp), ("behavior_packs", self.var_bp)):
            p = os.path.join(d, sub)
            if os.path.isdir(p):
                var.set(os.path.normpath(p))
                found.append(sub)
        level = None
        try:
            with open(os.path.join(d, "server.properties"), encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip().startswith("level-name="):
                        level = line.split("=", 1)[1].strip()
                        break
        except OSError:
            pass
        if level and os.path.isdir(os.path.join(d, "worlds", level)):
            self.var_world.set(os.path.normpath(os.path.join(d, "worlds", level)))
            found.append(f"world '{level}'")
        if found:
            self.log(f"Auto-filled: {', '.join(found)}", "good")
        else:
            self.log("Didn't find resource_packs / behavior_packs in that folder.", "warn")
        self._save_cfg()

    def _dirs(self):
        return self.var_rp.get().strip(), self.var_bp.get().strip(), self.var_world.get().strip()

    def _schedule_refresh(self):
        if self._refresh_job:
            self.after_cancel(self._refresh_job)
        self._refresh_job = self.after(400, self.refresh_all)

    def refresh_all(self):
        self._refresh_job = None
        self.refresh()
        self.refresh_installed()

    def run_bg(self, fn, done=None):
        """Run fn(log) on a worker thread; done(result) runs on the UI thread."""
        if self.busy:
            messagebox.showinfo(APP, "Still working on the last task — give it a moment.")
            return False
        self.busy = True
        self.config(cursor="watch")

        def log(m, t=""):
            self.q.put(("log", m, t))

        def worker():
            result = None
            try:
                result = fn(log)
            except Exception as e:
                log(f"✗ {e}", "bad")
            self.q.put(("done", done, result))

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.log(item[1], item[2])
                elif item[0] == "done":
                    self.busy = False
                    self.config(cursor="")
                    if item[1]:
                        item[1](item[2])
        except queue.Empty:
            pass
        self.after(100, self._poll)

    # ---- Deploy ----
    def pick_addon(self):
        start = self.cfg.get("last_addon_dir", "")
        files = filedialog.askopenfilenames(
            title="Select add-on(s)",
            filetypes=[("Bedrock add-ons", "*.mcaddon *.mcpack *.zip"), ("All files", "*.*")],
            initialdir=start if os.path.isdir(start) else None,
        )
        if files:
            self.cfg["last_addon_dir"] = os.path.dirname(files[0])
            self.load_addons(files)

    def load_addons(self, files, quiet_doctor=False):
        for z in self.archives:
            try:
                z.close()
            except Exception:
                pass
        self.archives, self.packs = [], []
        for f in files:
            try:
                zf = zipfile.ZipFile(f)
            except (zipfile.BadZipFile, OSError) as e:
                self.log(f"✗ {os.path.basename(f)}: not a valid add-on ({e})", "bad")
                continue
            self.archives.append(zf)
            packs, warns = scan_archive(zf, os.path.basename(f))
            for w in warns:
                self.log("  " + w, "warn")
            if not packs:
                self.log(f"✗ {os.path.basename(f)}: no packs (manifest.json) found inside", "bad")
            else:
                kinds = ", ".join(f"{p.name} [{KIND_LABEL[p.kind]}]" for p in packs)
                self.log(f"Loaded {os.path.basename(f)}: {kinds}")
            self.packs += packs
        names = [os.path.basename(f) for f in files]
        self.var_addon.set(names[0] if len(names) == 1 else f"{len(names)} files: " + ", ".join(names))
        self.refresh()
        if self.packs and not quiet_doctor:
            self.doctor_loaded(switch=False)

    def refresh(self):
        if self.busy:
            return
        rp, bp, _ = self._dirs()
        plan(self.packs, rp, bp)
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.packs):
            if not p.include or p.kind not in DEPLOYABLE:
                tag = "off"
            elif not p.target_dir:
                tag = "warn"
            elif p.status.startswith("New"):
                tag = "new"
            else:
                tag = "update"
            self.tree.insert("", "end", iid=str(i), tags=(tag,), values=(
                "☑" if p.include else "☐", p.name, KIND_LABEL[p.kind], ver_str(p.version),
                p.status if p.include or p.kind not in DEPLOYABLE else "Excluded",
            ))

    def _on_click(self, event):
        if self.tree.identify_region(event.x, event.y) == "cell" and \
                self.tree.identify_column(event.x) == "#1":
            row = self.tree.identify_row(event.y)
            if row:
                self._toggle([row])
                return "break"

    def _toggle(self, rows):
        for r in rows:
            p = self.packs[int(r)]
            if p.kind in DEPLOYABLE:
                p.include = not p.include
        self.refresh()

    def deploy(self):
        if self.busy:
            return
        sel = [p for p in self.packs if p.include and p.kind in DEPLOYABLE]
        if not sel:
            messagebox.showinfo(APP, "Nothing to deploy — load an add-on and tick at least one pack.")
            return
        rp, bp, world = self._dirs()
        plan(self.packs, rp, bp)
        missing = [p for p in sel if not p.target_dir]
        if missing:
            messagebox.showwarning(APP, "Set a folder for:\n\n" + "\n".join(
                f"• {p.name} ({KIND_LABEL[p.kind]})" for p in missing))
            return
        if world and not os.path.isdir(world):
            messagebox.showwarning(APP, "The world folder doesn't exist. Clear it or pick a valid one.")
            return

        updates = sum(1 for p in sel if not p.status.startswith("New"))
        summary = f"Deploy {len(sel)} pack(s)?\n\n{updates} update/replace, {len(sel) - updates} new."
        sel_ids = {p.uuid for p in sel}
        errs = [i for i in self.doc_issues if i.sev == "Error" and i.target.pack is not None
                and i.target.uuid in sel_ids]
        if errs:
            summary += (f"\n\n⚠ PackDoctor found {len(errs)} error(s) in these packs — they may not "
                        f"work fully. See the PackDoctor tab.")
        if world:
            summary += f"\n\nWorld pack lists in:\n{world}"
        summary += "\n\nTip: stop the server first so files aren't locked."
        if not messagebox.askyesno(APP, summary):
            return

        self._save_cfg()
        backup, add_new = self.var_backup.get(), self.var_addnew.get()
        self.deploy_btn.config(state="disabled", text="Deploying…")
        self.log("─" * 60, "dim")

        def job(log):
            done = []
            for p in sel:
                try:
                    n = install_pack(p, backup, log)
                    done.append(p)
                    log(f"✓ {p.name} → {os.path.join(p.target_dir, p.target_folder)}  ({n} files)", "good")
                except PermissionError as e:
                    log(f"✗ {p.name}: access denied — is the server still running? ({e})", "bad")
                except Exception as e:
                    log(f"✗ {p.name}: {e}", "bad")
            if world and done:
                update_world(world, done, add_new, log)
            log(f"Done — {len(done)}/{len(sel)} pack(s) deployed.",
                "good" if len(done) == len(sel) else "warn")

        def finished(_):
            self.deploy_btn.config(state="normal", text="Deploy to server")
            self.refresh_all()

        if not self.run_bg(job, finished):
            self.deploy_btn.config(state="normal", text="Deploy to server")

    # ---- Installed ----
    def refresh_installed(self):
        if self.busy:
            return
        rp, bp, world = self._dirs()
        self.installed = list_installed(rp, "resource") + list_installed(bp, "behavior")
        wu = world_uuids(world) if world and os.path.isdir(world) else None
        self.itree.delete(*self.itree.get_children())
        for i, ip in enumerate(self.installed):
            if wu is None:
                ip.in_world, wtxt = None, "—"
            else:
                ip.in_world = ip.uuid in wu[ip.kind]
                wtxt = "Yes" if ip.in_world else "No"
            tag = "off" if ip.in_world is False else ""
            self.itree.insert("", "end", iid=str(i), tags=(tag,), values=(
                ip.name, KIND_LABEL[ip.kind], ver_str(ip.version), ip.folder, wtxt))

    def _selected_installed(self):
        return [self.installed[int(r)] for r in self.itree.selection()]

    def open_selected(self):
        sel = self._selected_installed()
        if sel:
            self._open(sel[0].path)
        else:
            messagebox.showinfo(APP, "Select a pack first.")

    def remove_selected(self):
        sel = self._selected_installed()
        if not sel:
            messagebox.showinfo(APP, "Select one or more packs to remove.")
            return
        _, _, world = self._dirs()
        chosen = {p.uuid: p for p in sel}
        # the other half of an add-on (BP <-> RP linked by dependencies)
        linked = [p for p in self.installed if p.uuid not in chosen and
                  (any(p.uuid in c.deps for c in sel) or any(c.uuid in p.deps for c in sel))]
        if linked:
            names = "\n".join(f"• {p.name} ({KIND_LABEL[p.kind]})" for p in linked)
            ans = messagebox.askyesnocancel(
                APP, f"These packs are linked to what you selected (the other half of the add-on):\n\n"
                     f"{names}\n\nRemove them too?")
            if ans is None:
                return
            if ans:
                chosen.update({p.uuid: p for p in linked})
        removing = list(chosen.values())
        dependents = [p for p in self.installed if p.uuid not in chosen and p.deps & set(chosen)]

        msg = "Remove these packs from the server?\n\n" + "\n".join(
            f"• {p.name} ({KIND_LABEL[p.kind]}, {ver_str(p.version)})" for p in removing)
        msg += ("\n\nOld files will be moved to pack_backups." if self.var_backup.get()
                else "\n\nFiles will be permanently deleted (backups are off).")
        if world:
            msg += "\nThey'll also be removed from the world's pack lists."
        else:
            msg += "\n\n⚠ No world folder set — the world's pack lists won't be cleaned up."
        if dependents:
            msg += "\n\n⚠ Still installed and depending on these:\n" + "\n".join(
                f"• {p.name}" for p in dependents)
        msg += "\n\nTip: stop the server first."
        if not messagebox.askyesno(APP, msg, icon="warning"):
            return

        backup = self.var_backup.get()
        self.log("─" * 60, "dim")

        def job(log):
            gone = []
            for p in removing:
                try:
                    remove_installed(p, backup, log)
                    gone.append(p)
                except PermissionError as e:
                    log(f"✗ {p.name}: access denied — is the server still running? ({e})", "bad")
                except Exception as e:
                    log(f"✗ {p.name}: {e}", "bad")
            if world and os.path.isdir(world) and gone:
                remove_from_world(world, gone, log)
            log(f"Done — removed {len(gone)}/{len(removing)} pack(s).",
                "good" if len(gone) == len(removing) else "warn")

        self.run_bg(job, lambda _: self.refresh_all())

    def set_in_world(self, enable):
        sel = self._selected_installed()
        if not sel:
            messagebox.showinfo(APP, "Select one or more packs first.")
            return
        _, _, world = self._dirs()
        if not world or not os.path.isdir(world):
            messagebox.showwarning(APP, "Set the world folder first.")
            return
        self.log("─" * 60, "dim")
        if enable:
            self.run_bg(lambda log: update_world(world, sel, True, log), lambda _: self.refresh_installed())
        else:
            self.run_bg(lambda log: remove_from_world(world, sel, log), lambda _: self.refresh_installed())

    # ---- PackDoctor ----
    def _known_uuids(self):
        return {p.uuid for p in self.installed} | {p.uuid for p in self.packs}

    def doctor_loaded(self, switch=True):
        targets = [Target.from_pack(p) for p in self.packs if p.kind in DEPLOYABLE]
        if not targets:
            if switch:
                messagebox.showinfo(APP, "Load an add-on on the Deploy tab first.")
            return
        self._doctor_run(targets, f"Loaded add-on: {self.var_addon.get()}", switch)

    def doctor_installed(self):
        sel = self._selected_installed()
        if not sel:
            self.nb.select(1)
            messagebox.showinfo(APP, "Select one or more packs on the Installed tab first.")
            return
        self._doctor_run([Target.from_installed(p) for p in sel],
                         f"Installed: {', '.join(p.name for p in sel)[:80]}", True)

    def doctor_file(self):
        f = filedialog.askopenfilename(
            title="Select an add-on to check",
            filetypes=[("Bedrock add-ons", "*.mcaddon *.mcpack *.zip"), ("All files", "*.*")])
        if not f:
            return
        self.load_addons([f], quiet_doctor=True)
        self.doctor_loaded()

    def _doctor_run(self, targets, label, switch):
        known = self._known_uuids()

        def job(log):
            issues, fixes = [], {}
            for t in targets:
                iss, fx = doctor_scan(t, known)
                issues += iss
                if fx:
                    fixes[id(t)] = fx
            return issues, fixes

        def finished(res):
            if not res:
                return
            self.doc_targets = targets
            self.doc_issues, self.doc_fixes = res
            self.var_doc_src.set(label)
            self._fill_doctor()
            errs = sum(1 for i in self.doc_issues if i.sev == "Error")
            fix = sum(1 for i in self.doc_issues if i.auto)
            warns = sum(1 for i in self.doc_issues if i.sev == "Warning")
            if errs or warns or fix:
                self.log(f"PackDoctor: {errs} error(s), {warns} warning(s), {fix} auto-fixable — "
                         f"see the PackDoctor tab.", "warn" if errs else "")
            else:
                self.log("PackDoctor: no known problems found.", "good")
            if switch:
                self.nb.select(2)

        self.run_bg(job, finished)

    def _fill_doctor(self):
        self.dtree.delete(*self.dtree.get_children())
        tags = {"Error": "bad", "Warning": "warn", "Fixable": "fix", "Info": "info"}
        order = sorted(range(len(self.doc_issues)), key=lambda i: (
            SEV_ORDER[self.doc_issues[i].sev], self.doc_issues[i].target.name, self.doc_issues[i].file))
        for i in order:
            it = self.doc_issues[i]
            sev = "Auto-fix" if it.auto else it.sev
            self.dtree.insert("", "end", iid=str(i), tags=(tags[it.sev],),
                              values=(sev, it.target.name, it.file, it.msg))
        if not self.doc_issues:
            self.dtree.insert("", "end", iid="none", tags=("good",),
                              values=("OK", "", "", "No known problems found in these packs"))
        nfix = sum(len(v) for v in self.doc_fixes.values())
        manual = sum(1 for i in self.doc_issues if i.sev in ("Error", "Warning"))
        self.var_doc_sum.set(f"{nfix} file(s) can be auto-fixed · {manual} need manual work")
        can_fix = bool(self.doc_fixes)
        self.export_btn.config(state="normal" if can_fix else "disabled")
        in_place = can_fix and any(t.in_place for t in self.doc_targets if id(t) in self.doc_fixes)
        self.inplace_btn.config(state="normal" if in_place else "disabled")
        self._set_detail("Select an issue to see what it means and how to fix it.")

    def _set_detail(self, text):
        self.ddetail.config(state="normal")
        self.ddetail.delete("1.0", "end")
        self.ddetail.insert("1.0", text)
        self.ddetail.config(state="disabled")

    def _show_issue(self, _=None):
        sel = self.dtree.selection()
        if not sel or sel[0] == "none":
            return
        it = self.doc_issues[int(sel[0])]
        head = f"{it.target.name} — {it.file}\n{it.msg}\n\n"
        self._set_detail(head + (it.detail or ""))

    def doctor_export(self):
        if not self.doc_fixes:
            return
        base = safe_folder_name(self.doc_targets[0].name, "fixed") if self.doc_targets else "fixed"
        out = filedialog.asksaveasfilename(
            title="Save fixed add-on as", defaultextension=".mcaddon",
            initialfile=f"{base} (fixed).mcaddon", filetypes=[("Bedrock add-on", "*.mcaddon")])
        if not out:
            return
        targets, fixes = self.doc_targets, self.doc_fixes

        def job(log):
            export_fixed(targets, fixes, out)
            log(f"✓ Saved fixed copy: {out}", "good")
            return out

        def finished(path):
            if path and messagebox.askyesno(APP, "Fixed copy saved. Load it on the Deploy tab now?"):
                self.load_addons([path], quiet_doctor=True)
                self.nb.select(0)

        self.run_bg(job, finished)

    def doctor_inplace(self):
        todo = [t for t in self.doc_targets if t.in_place and id(t) in self.doc_fixes]
        if not todo:
            return
        msg = "Apply auto-fixes directly to these installed packs?\n\n" + "\n".join(
            f"• {t.name} ({len(self.doc_fixes[id(t)])} file(s))" for t in todo)
        msg += "\n\nEach pack is copied to pack_backups first.\nTip: stop the server first."
        if not messagebox.askyesno(APP, msg):
            return
        fixes = self.doc_fixes
        self.log("─" * 60, "dim")

        def job(log):
            for t in todo:
                try:
                    apply_fixes_in_place(t, fixes[id(t)], log)
                except Exception as e:
                    log(f"✗ {t.name}: {e}", "bad")

        def finished(_):
            self.refresh_installed()
            self._doctor_run([Target(t.name, t.kind, t.uuid, t.version, t.label, path=t.path)
                              for t in self.doc_targets if t.in_place],
                             self.var_doc_src.get(), False)

        self.run_bg(job, finished)


def main():
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            # own taskbar identity, so Windows shows our icon instead of python.exe's
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("NullHarp.PackDeploy")
        except Exception:
            pass
    app = App()
    files = [a for a in sys.argv[1:] if os.path.isfile(a)]
    if files:  # supports "Open with" / right-click "Deploy with PackDeploy"
        app.after(200, lambda: app.load_addons(files))
    app.mainloop()


if __name__ == "__main__":
    main()
