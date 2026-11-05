#!/usr/bin/env python3
"""
PackDeploy — install / update Minecraft Bedrock add-ons on a dedicated server.

Load one or more .mcaddon / .mcpack / .zip files, point it at your server's
resource_packs and behavior_packs folders (and optionally the world folder),
and it extracts each pack to the right place. Packs that are already installed
(matched by manifest UUID) are replaced in their existing folder, with an
optional backup, and the world's pack lists get their version numbers updated.

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
import io
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
import uuid as uuidlib
import zipfile
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP = "PackDeploy"
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

KIND_LABEL = {
    "resource": "Resource",
    "behavior": "Behavior",
    "skin": "Skin pack",
    "world": "World template",
    "unknown": "Unknown",
}
DEPLOYABLE = ("resource", "behavior")


# --------------------------------------------------------------------------- #
#  JSON helpers (Bedrock manifests often have comments / trailing commas)
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
    data = data.lstrip("\ufeff")
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


def safe_folder_name(name, pack_uuid):
    name = re.sub(r"§.", "", name or "")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    name = name[:60].strip(" .")
    return name or f"pack_{pack_uuid[:8]}"


# --------------------------------------------------------------------------- #
#  Pack discovery
# --------------------------------------------------------------------------- #
def detect_kind(manifest):
    types = {
        str(m.get("type", "")).lower()
        for m in manifest.get("modules", [])
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
        self.name = self._resolve_name(str(header.get("name", "") or ""))
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

    def _resolve_name(self, raw):
        name = raw
        if raw.startswith("pack."):  # localized name, e.g. "pack.name"
            lang = self.read_member("texts/en_US.lang")
            if lang:
                for line in lang.decode("utf-8-sig", "replace").splitlines():
                    if line.startswith(raw + "="):
                        name = line.split("=", 1)[1].split("\t")[0].split("##")[0].strip()
                        break
        name = re.sub(r"§.", "", name).strip()
        if not name or name.startswith("pack."):
            folder = os.path.basename(self.prefix.rstrip("/"))
            name = folder or os.path.splitext(self.source.split(" › ")[-1])[0]
        return name

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
#  Planning / installing
# --------------------------------------------------------------------------- #
def index_installed(folder):
    """uuid -> (folder name, version) for packs already in a packs folder."""
    idx = {}
    if not folder or not os.path.isdir(folder):
        return idx
    for entry in os.scandir(folder):
        if not entry.is_dir() or entry.name.startswith(".packdeploy_tmp_"):
            continue
        mp = os.path.join(entry.path, "manifest.json")
        if not os.path.isfile(mp):
            continue
        try:
            h = read_json_file(mp, {}).get("header", {}) or {}
            u = str(h.get("uuid", "")).lower()
            if u:
                idx[u] = (entry.name, h.get("version"))
        except Exception:
            pass
    return idx


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
                parent = os.path.dirname(os.path.abspath(d))
                bdir = os.path.join(parent, "pack_backups", os.path.basename(os.path.normpath(d)))
                os.makedirs(bdir, exist_ok=True)
                stamp = time.strftime("%Y%m%d-%H%M%S")
                bpath = os.path.join(bdir, f"{p.target_folder}_v{ver_str(p.old_version)}_{stamp}")
                shutil.move(final, bpath)
                log(f"  old version backed up to {bpath}", "dim")
            else:
                shutil.rmtree(final)
        os.rename(tmp, final)
        return count
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def update_world(world_dir, done, add_new, log):
    files = (("behavior", "world_behavior_packs.json"), ("resource", "world_resource_packs.json"))
    for kind, fname in files:
        mine = [p for p in done if p.kind == kind]
        if not mine:
            continue
        path = os.path.join(world_dir, fname)
        try:
            data = read_json_file(path, [])
        except Exception as e:
            log(f"✗ Couldn't read {fname} ({e}) — left untouched", "bad")
            continue
        if not isinstance(data, list):
            log(f"✗ {fname} isn't a list — left untouched", "bad")
            continue

        changed = False
        for p in mine:
            entry = next(
                (e for e in data if isinstance(e, dict)
                 and str(e.get("pack_id", "")).lower() == p.uuid),
                None,
            )
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

        if not changed:
            log(f"  {fname} already up to date", "dim")
            continue
        if os.path.exists(path):
            backup_file(path, log)
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            log(f"✓ Updated {fname}", "good")
        except OSError as e:
            log(f"✗ Couldn't write {fname} ({e}). The pack files are installed, but the world "
                f"won't load them until this file is updated. Check that your Windows user "
                f"can write to this share / that the file isn't read-only.", "bad")


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
#  GUI
# --------------------------------------------------------------------------- #
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP)
        self.geometry("960x660")
        self.minsize(780, 540)
        self.configure(bg=BG)
        self._set_icon()

        self.cfg = self._load_cfg()
        self.packs = []
        self.archives = []
        self.q = queue.Queue()
        self.busy = False

        self.var_addon = tk.StringVar(value="No add-on loaded")
        self.var_rp = tk.StringVar(value=self.cfg.get("rp", ""))
        self.var_bp = tk.StringVar(value=self.cfg.get("bp", ""))
        self.var_world = tk.StringVar(value=self.cfg.get("world", ""))
        self.var_backup = tk.BooleanVar(value=self.cfg.get("backup", True))
        self.var_addnew = tk.BooleanVar(value=self.cfg.get("add_new", True))

        self._style()
        self._build()
        for v in (self.var_rp, self.var_bp):
            v.trace_add("write", lambda *_: self.refresh())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll)
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

    def _btn(self, parent, text, cmd, accent=False):
        b = tk.Button(parent, text=text, command=cmd, relief="flat", bd=0, cursor="hand2",
                      bg=ACCENT if accent else "#3a3a3d", fg="white" if accent else FG,
                      activebackground="#2f66b3" if accent else "#4a4a4e", activeforeground="white",
                      padx=14, pady=5, font=("Segoe UI", 10, "bold" if accent else "normal"))
        return b

    def _check(self, parent, text, var):
        return tk.Checkbutton(parent, text=text, variable=var, bg=BG, fg=FG, selectcolor=FIELD,
                              activebackground=BG, activeforeground=FG, font=("Segoe UI", 10),
                              highlightthickness=0, bd=0)

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=14, pady=(14, 4))
        top.columnconfigure(1, weight=1)

        rows = [
            ("Add-on file(s)", self.var_addon, self.pick_addon, True),
            ("Resource packs folder", self.var_rp,
             lambda: self.pick_dir(self.var_rp, "Select the server's resource_packs folder"), False),
            ("Behavior packs folder", self.var_bp,
             lambda: self.pick_dir(self.var_bp, "Select the server's behavior_packs folder"), False),
            ("World folder (optional)", self.var_world,
             lambda: self.pick_dir(self.var_world, "Select the world folder (worlds\\<name>)"), False),
        ]
        for r, (label, var, cmd, ro) in enumerate(rows):
            tk.Label(top, text=label, bg=BG, fg=FG, font=("Segoe UI", 10), anchor="w") \
                .grid(row=r, column=0, sticky="w", pady=3, padx=(0, 10))
            e = tk.Entry(top, textvariable=var, bg=FIELD, fg=FG, insertbackground=FG, relief="flat",
                         highlightthickness=1, highlightbackground=BORDER, highlightcolor=ACCENT,
                         font=("Segoe UI", 10), readonlybackground=FIELD)
            if ro:
                e.config(state="readonly")
            e.grid(row=r, column=1, sticky="ew", ipady=4)
            self._btn(top, "Browse…", cmd).grid(row=r, column=2, padx=(8, 0), sticky="ew")

        opts = tk.Frame(self, bg=BG)
        opts.pack(fill="x", padx=14, pady=(4, 8))
        self._btn(opts, "Auto-fill from server folder…", self.pick_server).pack(side="left")
        self._check(opts, "Back up replaced packs", self.var_backup).pack(side="left", padx=(16, 0))
        self._check(opts, "Add new packs to world", self.var_addnew).pack(side="left", padx=(12, 0))

        # pack list
        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="both", expand=True, padx=14)
        cols = ("use", "name", "type", "version", "action")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="extended")
        for c, text, w, stretch in (
            ("use", "✓", 40, False), ("name", "Pack", 240, True), ("type", "Type", 90, False),
            ("version", "Version", 80, False), ("action", "What will happen", 360, True),
        ):
            self.tree.heading(c, text=text)
            self.tree.column(c, width=w, stretch=stretch, anchor="center" if c in ("use", "version") else "w")
        self.tree.tag_configure("new", foreground=GOOD)
        self.tree.tag_configure("update", foreground="#7fb4ff")
        self.tree.tag_configure("off", foreground=DIM)
        self.tree.tag_configure("warn", foreground=WARN)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Button-1>", self._on_click)
        self.tree.bind("<space>", lambda e: self._toggle(self.tree.selection()))

        # actions
        act = tk.Frame(self, bg=BG)
        act.pack(fill="x", padx=14, pady=8)
        self.deploy_btn = self._btn(act, "Deploy to server", self.deploy, accent=True)
        self.deploy_btn.pack(side="left")
        self._btn(act, "Open resource_packs", lambda: self._open(self.var_rp.get())).pack(side="left", padx=(8, 0))
        self._btn(act, "Open behavior_packs", lambda: self._open(self.var_bp.get())).pack(side="left", padx=(8, 0))
        tk.Label(act, text="Click ✓ or press Space to include/exclude a pack", bg=BG, fg=DIM,
                 font=("Segoe UI", 9)).pack(side="right")

        # log
        self.logbox = tk.Text(self, height=9, bg=PANEL, fg=FG, relief="flat", wrap="word",
                              font=("Consolas", 9), highlightthickness=0, padx=8, pady=6)
        self.logbox.pack(fill="x", padx=14, pady=(0, 14))
        for tag, color in (("good", GOOD), ("bad", BAD), ("warn", WARN), ("dim", DIM)):
            self.logbox.tag_configure(tag, foreground=color)
        self.logbox.config(state="disabled")

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

    def load_addons(self, files):
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

    def refresh(self):
        if self.busy:
            return
        plan(self.packs, self.var_rp.get().strip(), self.var_bp.get().strip())
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

    # ---- deploy ----
    def deploy(self):
        if self.busy:
            return
        sel = [p for p in self.packs if p.include and p.kind in DEPLOYABLE]
        if not sel:
            messagebox.showinfo(APP, "Nothing to deploy — load an add-on and tick at least one pack.")
            return
        plan(self.packs, self.var_rp.get().strip(), self.var_bp.get().strip())
        missing = [p for p in sel if not p.target_dir]
        if missing:
            messagebox.showwarning(APP, "Set a folder for:\n\n" + "\n".join(
                f"• {p.name} ({KIND_LABEL[p.kind]})" for p in missing))
            return
        world = self.var_world.get().strip()
        if world and not os.path.isdir(world):
            messagebox.showwarning(APP, "The world folder doesn't exist. Clear it or pick a valid one.")
            return

        updates = sum(1 for p in sel if not p.status.startswith("New"))
        summary = f"Deploy {len(sel)} pack(s)?\n\n{updates} update/replace, {len(sel) - updates} new."
        if world:
            summary += f"\n\nWorld pack lists in:\n{world}"
        summary += "\n\nTip: stop the server first so files aren't locked."
        if not messagebox.askyesno(APP, summary):
            return

        self._save_cfg()
        self.busy = True
        self.deploy_btn.config(state="disabled", text="Deploying…")
        self.log("─" * 60, "dim")
        threading.Thread(target=self._worker, daemon=True,
                         args=(sel, self.var_backup.get(), world, self.var_addnew.get())).start()

    def _worker(self, sel, backup, world, add_new):
        def log(m, t=""):
            self.q.put(("log", m, t))

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
            try:
                update_world(world, done, add_new, log)
            except Exception as e:
                log(f"✗ World update failed: {e}", "bad")
        log(f"Done — {len(done)}/{len(sel)} pack(s) deployed.",
            "good" if len(done) == len(sel) else "warn")
        self.q.put(("finished",))

    def _poll(self):
        try:
            while True:
                item = self.q.get_nowait()
                if item[0] == "log":
                    self.log(item[1], item[2])
                elif item[0] == "finished":
                    self.busy = False
                    self.deploy_btn.config(state="normal", text="Deploy to server")
                    self.refresh()
        except queue.Empty:
            pass
        self.after(100, self._poll)


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
    if files:  # supports "Open with" / dragging a file onto the .exe
        app.after(200, lambda: app.load_addons(files))
    app.mainloop()


if __name__ == "__main__":
    main()
