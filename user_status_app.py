"""MarryMatch - User Status desktop tool.

Tab "Users":  see who is active now, set a user online/offline, ban/unban, search.
Tab "Groups": organise users into groups, activate a percentage of a group,
              force single users ON/OFF (manual override) and set an auto-off timer.

Every status change is a plain update of profiles.last_active_at (or user_status.is_banned)
in Supabase - exactly like editing the row by hand. Nothing else is created in your database.
Groups and running timers are saved on THIS computer (~/.marrymatch_status_groups.json).
Timers are checked by this app, so they switch users off while the app is open (and the
moment you open it again, if one ran out while it was closed).

Needs only Python 3 (tkinter ships with the normal python.org installers).

Run:  python user_status_app.py
Key:  paste your Supabase *service_role* (secret) key in the app, or set env var
      SUPABASE_SERVICE_KEY. The key is kept in memory only and never saved to disk.
"""
import json
import os
import random
import tkinter as tk
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, ttk

DEFAULT_URL = "https://zaixmvhfblgarpjckjhc.supabase.co"
DATA_FILE = os.environ.get("MARRYMATCH_STATUS_DATA") or os.path.join(
    os.path.expanduser("~"), ".marrymatch_status_groups.json")
ONLINE_WINDOW_MIN = 5      # active within this many minutes => "Online"
AUTO_REFRESH_MS = 10_000
DEFAULT_TIMER_MIN = 20


def now_z(delta=None):
    t = datetime.now(timezone.utc) + (delta or timedelta())
    return t.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def ago(delta):
    s = int(delta.total_seconds())
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def left(seconds):
    s = max(int(seconds), 0)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def target_count(eligible, forced, pct):
    """Round half up (10% of 25 = 3). Forced-ON members count toward the total."""
    return max((eligible * pct * 2 + 100) // 200, forced)


class Supabase:
    """Plain REST calls: read profiles, update one column. Nothing else."""

    def __init__(self, url, key):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.headers = {
            "apikey": key,
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
        }

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data,
                                     headers=self.headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')}")

    def users(self):
        profiles = self._call(
            "GET", "profiles?select=id,display_name,last_active_at,"
                   "hide_activity,incognito&order=last_active_at.desc")
        status = self._call("GET", "user_status?select=user_id,is_banned")
        banned = {s["user_id"]: s["is_banned"] for s in status}
        for p in profiles:
            p["is_banned"] = banned.get(p["id"], False)
        return profiles

    def set_last_active(self, uid, when_iso):
        self._call("PATCH", f"profiles?id=eq.{uid}", {"last_active_at": when_iso})

    def restore_last_active(self, uid, set_value, prev_value):
        """Put the old value back - but only if nobody changed it since we set it
        (so a user's genuine activity is never overwritten)."""
        self._call("PATCH", f"profiles?id=eq.{uid}&last_active_at=eq.{set_value}",
                   {"last_active_at": prev_value})

    def set_banned(self, uid, value):
        self._call("PATCH", f"user_status?user_id=eq.{uid}", {"is_banned": value})


class Store:
    """Groups + running timers, kept in a small JSON file on this computer."""

    def __init__(self, path):
        self.path = path
        self.groups = []     # {"id", "name", "members": {user_id: "on" | "off" | None}}
        self.active = {}     # user_id -> {"group", "source", "set_value", "prev_value", "expires_at"}
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            self.groups = d.get("groups", [])
            self.active = d.get("active", {})
        except FileNotFoundError:
            pass
        except (ValueError, OSError):
            try:
                os.replace(path, path + ".bak")      # keep a broken file, start fresh
            except OSError:
                pass

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"groups": self.groups, "active": self.active}, f, indent=1)
        os.replace(tmp, self.path)

    def group(self, gid):
        return next((g for g in self.groups if g["id"] == gid), None)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MarryMatch - User Status")
        self.geometry("1040x640")
        self.minsize(900, 560)
        self.api = None
        self.store = Store(DATA_FILE)
        self.users = []
        self.user_by_id = {}
        self.rows = {}
        self.after_id = None

        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="URL").grid(row=0, column=0, sticky="w")
        self.url = ttk.Entry(top, width=44)
        self.url.insert(0, DEFAULT_URL)
        self.url.grid(row=0, column=1, padx=4)
        ttk.Label(top, text="Service key").grid(row=0, column=2, sticky="w")
        self.key = ttk.Entry(top, width=30, show="*")
        self.key.insert(0, os.environ.get("SUPABASE_SERVICE_KEY", ""))
        self.key.grid(row=0, column=3, padx=4)
        ttk.Button(top, text="Connect", command=self.connect).grid(row=0, column=4)

        self.msg = ttk.Label(self, text="Enter your service key and press Connect.",
                             anchor="w", padding=(8, 2))
        self.msg.pack(side="bottom", fill="x")

        self.nb = ttk.Notebook(self)
        self.nb.pack(fill="both", expand=True)
        self.build_users_tab()
        self.build_groups_tab()

        if self.key.get():
            self.connect()
        self.tick()

    # ================================================================= helpers
    def state_of(self, uid, now):
        """-> (state, source, seconds_left or None) for one user."""
        u = self.user_by_id.get(uid)
        if not u:
            return "?", "", None
        a = self.store.active.get(uid)
        exp = parse_ts(a["expires_at"]) if a and a.get("expires_at") else None
        running = a is not None and (exp is None or exp > now)
        recent = now - parse_ts(u["last_active_at"]) < timedelta(minutes=ONLINE_WINDOW_MIN)
        secs = (exp - now).total_seconds() if running and exp else None
        if u["is_banned"]:
            return "banned", "", None
        if running or recent:
            return "ONLINE", (a["source"] if running else ""), secs
        return "offline", "", None

    def selected_user(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("Select a user", "Click a user in the list first.")
            return None
        return self.rows[sel[0]]

    def act(self, fn):
        try:
            fn()
        except Exception as e:
            messagebox.showerror("Update failed", str(e))
        finally:
            self.store.save()
        self.refresh()

    # ================================================================= connect / refresh / timers
    def connect(self):
        if not self.key.get().strip():
            messagebox.showwarning("Key needed", "Paste your Supabase service_role key.")
            return
        self.api = Supabase(self.url.get().strip(), self.key.get().strip())
        self.refresh(manual=True)

    def refresh(self, manual=False):
        if self.after_id:
            self.after_cancel(self.after_id)
            self.after_id = None
        if not self.api:
            return
        try:
            self.load_users()
            self.expire_due()
        except Exception as e:
            self.msg.config(text="Could not load users: " + str(e)[:120])
            if manual:
                messagebox.showerror("Could not load users", str(e))
            self.after_id = self.after(AUTO_REFRESH_MS, self.refresh)
            return
        self.render_users()
        self.render_groups()
        self.after_id = self.after(AUTO_REFRESH_MS, self.refresh)

    def load_users(self):
        self.users = self.api.users()
        self.user_by_id = {u["id"]: u for u in self.users}

    def expire_due(self):
        """Switch off every user whose timer ran out. Returns how many."""
        now = datetime.now(timezone.utc)
        due = [uid for uid, a in self.store.active.items()
               if a.get("expires_at") and parse_ts(a["expires_at"]) <= now]
        try:
            for uid in due:
                self.release_user(uid)
        finally:
            if due:
                self.store.save()
        return len(due)

    def tick(self):
        """Every second: timers + countdown text (network only when a timer ran out)."""
        try:
            if self.api:
                now = datetime.now(timezone.utc)
                if any(a.get("expires_at") and parse_ts(a["expires_at"]) <= now
                       for a in self.store.active.values()):
                    self.refresh()
                for uid in self.member_tree.get_children():
                    _, _, secs = self.state_of(uid, now)
                    a = self.store.active.get(uid)
                    self.member_tree.set(uid, "left", left(secs) if secs is not None else (
                        "no limit" if a and not a.get("expires_at") else ""))
        except Exception as e:                       # never let the timer loop die
            self.msg.config(text="Timer check failed: " + str(e)[:120])
        self.after(1000, self.tick)

    # ---- the two plain Supabase operations behind every group action
    def activate_user(self, uid, gid, source, expires):
        a = self.store.active.get(uid)
        if a:                                        # already active: just refresh label + timer
            a.update(group=gid, source=source, expires_at=expires)
            return
        u = self.user_by_id.get(uid)
        if not u:
            return
        v = now_z()
        self.api.set_last_active(uid, v)             # plain update: last_active_at = now
        self.store.active[uid] = {"group": gid, "source": source, "set_value": v,
                                  "prev_value": u["last_active_at"], "expires_at": expires}
        u["last_active_at"] = v                      # keep the in-memory copy in step

    def release_user(self, uid):
        a = self.store.active.get(uid)
        if not a:
            return
        self.api.restore_last_active(uid, a["set_value"], a["prev_value"])   # plain update
        self.store.active.pop(uid, None)
        u = self.user_by_id.get(uid)
        if u and parse_ts(u["last_active_at"]) == parse_ts(a["set_value"]):
            u["last_active_at"] = a["prev_value"]    # keep the in-memory copy in step

    # ================================================================= users tab
    def build_users_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  Users  ")
        sb = ttk.Frame(tab, padding=(8, 8, 8, 0))
        sb.pack(fill="x")
        ttk.Label(sb, text="Search").pack(side="left")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.render_users())
        ttk.Entry(sb, textvariable=self.query).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(sb, text="Clear", command=lambda: self.query.set("")).pack(side="left")
        ttk.Label(sb, text="(name, id, online / offline / banned)").pack(side="left", padx=6)

        cols = ("name", "status", "source", "last", "hidden", "banned")
        self.tree = ttk.Treeview(tab, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("name", "User", 190), ("status", "Status", 90),
                        ("source", "Group", 90), ("last", "Last active", 120),
                        ("hidden", "Hides activity", 100), ("banned", "Banned", 70)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("online", background="#d7f5dc")
        self.tree.tag_configure("banned", background="#f8d7da")
        self.tree.pack(fill="both", expand=True, padx=8, pady=8)

        bar = ttk.Frame(tab, padding=(8, 0, 8, 8))
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=lambda: self.refresh(manual=True)).pack(side="left")
        ttk.Button(bar, text="Set Online", command=self.set_online).pack(side="left", padx=6)
        ttk.Button(bar, text="Set Offline", command=self.set_offline).pack(side="left")
        ttk.Button(bar, text="Ban / Unban", command=self.toggle_ban).pack(side="left", padx=6)

    def render_users(self):
        now = datetime.now(timezone.utc)
        q = self.query.get().strip().lower()
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        online = shown = 0
        for u in self.users:
            state, src, _ = self.state_of(u["id"], now)
            online += state == "ONLINE"
            if q and not (q in u["display_name"].lower() or q in u["id"].lower()
                          or q == state.lower()):
                continue
            shown += 1
            self.rows[u["id"]] = u
            tags = ("banned",) if state == "banned" else (("online",) if state == "ONLINE" else ())
            self.tree.insert("", "end", iid=u["id"], tags=tags, values=(
                u["display_name"], state, src,
                ago(now - parse_ts(u["last_active_at"])),
                "yes" if u["hide_activity"] else "no",
                "YES" if u["is_banned"] else "no"))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self.msg.config(text=f"{online} online / {len(self.users)} users"
                             f"{f' - showing {shown}' if q else ''} - "
                             f"{now.astimezone():%H:%M:%S}")

    def _plain_set(self, uid, when_iso):
        self.api.set_last_active(uid, when_iso)
        self.store.active.pop(uid, None)             # a manual change replaces any group timer

    def set_online(self):
        u = self.selected_user()
        if u:
            self.act(lambda: self._plain_set(u["id"], now_z()))

    def set_offline(self):
        u = self.selected_user()
        if u:
            self.act(lambda: self._plain_set(u["id"], now_z(-timedelta(days=1))))

    def toggle_ban(self):
        u = self.selected_user()
        if not u:
            return
        new = not u["is_banned"]
        if messagebox.askyesno("Confirm", f"{'Ban' if new else 'Unban'} {u['display_name']}?"):
            self.act(lambda: self.api.set_banned(u["id"], new))

    # ================================================================= groups tab
    def build_groups_tab(self):
        tab = ttk.Frame(self.nb)
        self.nb.add(tab, text="  Groups  ")
        ttk.Label(tab, foreground="#555", padding=(8, 4), text=(
            "Groups are saved on this computer. Timers switch users off while this app is open "
            "(and as soon as you open it again if one ran out).")).pack(fill="x")
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        # ---- left: group list
        left_f = ttk.Frame(body)
        left_f.pack(side="left", fill="y")
        self.group_tree = ttk.Treeview(left_f, columns=("name", "count"), show="headings",
                                       selectmode="browse", height=14)
        self.group_tree.heading("name", text="Group")
        self.group_tree.heading("count", text="Active / total")
        self.group_tree.column("name", width=130)
        self.group_tree.column("count", width=100, anchor="center")
        self.group_tree.pack(fill="y", expand=True)
        self.group_tree.bind("<<TreeviewSelect>>", lambda _e: self.render_members())
        gb = ttk.Frame(left_f)
        gb.pack(fill="x", pady=6)
        ttk.Button(gb, text="New", command=self.new_group).pack(side="left")
        ttk.Button(gb, text="Rename", command=self.rename_group).pack(side="left", padx=4)
        ttk.Button(gb, text="Delete", command=self.delete_group).pack(side="left")

        # ---- right: members + controls
        right = ttk.Frame(body)
        right.pack(side="left", fill="both", expand=True, padx=(10, 0))

        sb = ttk.Frame(right)
        sb.pack(fill="x")
        ttk.Label(sb, text="Search members").pack(side="left")
        self.gquery = tk.StringVar()
        self.gquery.trace_add("write", lambda *_: self.render_members())
        ttk.Entry(sb, textvariable=self.gquery).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(sb, text="Add users...", command=self.add_users).pack(side="left")
        ttk.Button(sb, text="Remove", command=self.remove_user).pack(side="left", padx=4)

        cols = ("name", "status", "override", "source", "left")
        self.member_tree = ttk.Treeview(right, columns=cols, show="headings", selectmode="browse")
        for c, t, w in (("name", "User", 170), ("status", "Status", 80),
                        ("override", "Manual override", 110), ("source", "Source", 90),
                        ("left", "Time left", 80)):
            self.member_tree.heading(c, text=t)
            self.member_tree.column(c, width=w, anchor="w")
        self.member_tree.tag_configure("online", background="#d7f5dc")
        self.member_tree.tag_configure("banned", background="#f8d7da")
        self.member_tree.pack(fill="both", expand=True, pady=6)

        ctl = ttk.LabelFrame(right, text="Activate this group", padding=8)
        ctl.pack(fill="x")
        r1 = ttk.Frame(ctl)
        r1.pack(fill="x")
        ttk.Label(r1, text="Percent").pack(side="left")
        self.pct = tk.IntVar(value=10)
        ttk.Spinbox(r1, from_=0, to=100, width=5, textvariable=self.pct,
                    command=self.update_preview).pack(side="left", padx=4)
        ttk.Label(r1, text="Timer (minutes, 0 = no limit)").pack(side="left", padx=(16, 0))
        self.minutes = tk.IntVar(value=DEFAULT_TIMER_MIN)
        ttk.Spinbox(r1, from_=0, to=10080, width=6, textvariable=self.minutes).pack(side="left", padx=4)
        self.preview = ttk.Label(ctl, text="", font=("TkDefaultFont", 10, "bold"))
        self.preview.pack(anchor="w", pady=(6, 0))
        self.pct.trace_add("write", lambda *_: self.update_preview())
        r2 = ttk.Frame(ctl)
        r2.pack(fill="x", pady=(6, 0))
        ttk.Button(r2, text="Apply", command=lambda: self.apply(False)).pack(side="left")
        ttk.Button(r2, text="Re-roll", command=lambda: self.apply(True)).pack(side="left", padx=6)
        ttk.Button(r2, text="Stop now", command=self.stop_group).pack(side="left")
        r3 = ttk.Frame(ctl)
        r3.pack(fill="x", pady=(8, 0))
        ttk.Label(r3, text="Selected user:").pack(side="left")
        ttk.Button(r3, text="Force ON", command=lambda: self.set_override("on")).pack(side="left", padx=(6, 0))
        ttk.Button(r3, text="Force OFF", command=lambda: self.set_override("off")).pack(side="left", padx=6)
        ttk.Button(r3, text="Clear override", command=lambda: self.set_override(None)).pack(side="left")

    def cur_group(self):
        sel = self.group_tree.selection()
        return sel[0] if sel else None

    def need_group(self):
        g = self.cur_group()
        if not g:
            messagebox.showinfo("Select a group", "Click a group on the left first.")
        return g

    def render_groups(self):
        sel = self.cur_group()
        self.group_tree.delete(*self.group_tree.get_children())
        now = datetime.now(timezone.utc)
        for g in sorted(self.store.groups, key=lambda g: g["name"].lower()):
            ids = [u for u in g["members"] if u in self.user_by_id]
            act = sum(self.state_of(u, now)[0] == "ONLINE" for u in ids)
            self.group_tree.insert("", "end", iid=g["id"], values=(g["name"], f"{act} / {len(ids)}"))
        kids = self.group_tree.get_children()
        if sel and self.group_tree.exists(sel):
            self.group_tree.selection_set(sel)
        elif kids:
            self.group_tree.selection_set(kids[0])
        self.render_members()

    def render_members(self):
        g = self.store.group(self.cur_group())
        sel = self.member_tree.selection()
        self.member_tree.delete(*self.member_tree.get_children())
        if g:
            q = self.gquery.get().strip().lower()
            now = datetime.now(timezone.utc)
            for uid in sorted((u for u in g["members"] if u in self.user_by_id),
                              key=lambda u: self.user_by_id[u]["display_name"].lower()):
                u = self.user_by_id[uid]
                state, src, _ = self.state_of(uid, now)
                if q and not (q in u["display_name"].lower() or q in uid.lower()
                              or q == state.lower()):
                    continue
                ov = {"on": "forced ON", "off": "forced OFF"}.get(g["members"][uid], "")
                tags = ("banned",) if state == "banned" else (("online",) if state == "ONLINE" else ())
                self.member_tree.insert("", "end", iid=uid, tags=tags,
                                        values=(u["display_name"], state, ov, src, ""))
        if sel and self.member_tree.exists(sel[0]):
            self.member_tree.selection_set(sel[0])
        self.update_preview()

    def update_preview(self):
        g = self.store.group(self.cur_group())
        if not g:
            self.preview.config(text="")
            return
        try:
            pct = min(max(int(self.pct.get()), 0), 100)
        except (tk.TclError, ValueError):
            self.preview.config(text="")
            return
        elig = [u for u, ov in g["members"].items()
                if u in self.user_by_id and not self.user_by_id[u]["is_banned"] and ov != "off"]
        forced = sum(g["members"][u] == "on" for u in elig)
        t = target_count(len(elig), forced, pct)
        self.preview.config(text=f"{pct}% of {len(elig)} eligible = {t} user{'s' if t != 1 else ''}")

    def minutes_value(self):
        try:
            return max(int(self.minutes.get()), 0)
        except (tk.TclError, ValueError):
            return DEFAULT_TIMER_MIN

    # ---- group CRUD (local)
    def ask_name(self, title, initial=""):
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.resizable(False, False)
        var = tk.StringVar(value=initial)
        ttk.Label(win, text="Group name").pack(padx=12, pady=(12, 2), anchor="w")
        e = ttk.Entry(win, textvariable=var, width=34)
        e.pack(padx=12)
        e.focus_set()
        result = {"v": None}

        def ok(_e=None):
            result["v"] = var.get().strip()
            win.destroy()
        e.bind("<Return>", ok)
        ttk.Button(win, text="OK", command=ok).pack(pady=12)
        win.grab_set()
        self.wait_window(win)
        return result["v"] or None

    def name_taken(self, name, skip=None):
        return any(g["name"].lower() == name.lower() and g["id"] != skip for g in self.store.groups)

    def new_group(self):
        name = self.ask_name("New group")
        if not name:
            return
        if self.name_taken(name):
            messagebox.showwarning("Name taken", "A group with that name already exists.")
            return
        self.store.groups.append({"id": str(uuid.uuid4()), "name": name[:60], "members": {}})
        self.store.save()
        self.render_groups()
        self.group_tree.selection_set(self.store.groups[-1]["id"])

    def rename_group(self):
        gid = self.need_group()
        if not gid:
            return
        g = self.store.group(gid)
        name = self.ask_name("Rename group", g["name"])
        if not name:
            return
        if self.name_taken(name, gid):
            messagebox.showwarning("Name taken", "A group with that name already exists.")
            return
        g["name"] = name[:60]
        self.store.save()
        self.render_groups()

    def delete_group(self):
        gid = self.need_group()
        if gid and messagebox.askyesno(
                "Delete group", "Delete this group? Active users in it are switched off first."):
            def go():
                self.stop(gid)
                self.store.groups = [g for g in self.store.groups if g["id"] != gid]
            self.act(go)

    def add_users(self):
        gid = self.need_group()
        if not gid:
            return
        g = self.store.group(gid)
        win = tk.Toplevel(self)
        win.title("Add users to group")
        win.geometry("420x440")
        win.transient(self)
        q = tk.StringVar()
        top = ttk.Frame(win, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Search").pack(side="left")
        ttk.Entry(top, textvariable=q).pack(side="left", fill="x", expand=True, padx=6)
        tv = ttk.Treeview(win, columns=("name",), show="headings", selectmode="extended")
        tv.heading("name", text="Users not in this group (Ctrl/Shift-click for several)")
        tv.pack(fill="both", expand=True, padx=8)

        def fill(*_):
            tv.delete(*tv.get_children())
            for u in self.users:
                if u["id"] not in g["members"] and q.get().strip().lower() in u["display_name"].lower():
                    tv.insert("", "end", iid=u["id"], values=(u["display_name"],))
        q.trace_add("write", fill)
        fill()

        def add():
            ids = list(tv.selection())
            if not ids:
                return
            win.destroy()
            for u in ids:
                g["members"].setdefault(u, None)
            self.store.save()
            self.render_groups()
        ttk.Button(win, text="Add selected", command=add).pack(pady=8)

    def remove_user(self):
        gid = self.need_group()
        sel = self.member_tree.selection()
        if not gid:
            return
        if not sel:
            messagebox.showinfo("Select a user", "Click a member in the list first.")
            return
        uid = sel[0]
        if not messagebox.askyesno("Remove", f"Remove {self.user_by_id[uid]['display_name']} from this group?"):
            return

        def go():
            a = self.store.active.get(uid)
            if a and a["group"] == gid:              # switch them off first
                self.release_user(uid)
            self.store.group(gid)["members"].pop(uid, None)
        self.act(go)

    # ---- activation (plain Supabase updates)
    def stop(self, gid):
        n = 0
        for uid, a in list(self.store.active.items()):
            if a["group"] == gid:
                self.release_user(uid)
                n += 1
        return n

    def activate_group(self, gid, pct, minutes, reroll):
        """Exactly pct% of the group's eligible members become active.
        Eligible = not banned and not forced OFF. Forced-ON members are always active and count
        toward the total. The random pick is kept unless reroll."""
        self.load_users()
        g = self.store.group(gid)
        exp = now_z(timedelta(minutes=minutes)) if minutes > 0 else None
        mem = {u: ov for u, ov in g["members"].items() if u in self.user_by_id}
        elig = [u for u, ov in mem.items() if not self.user_by_id[u]["is_banned"] and ov != "off"]
        forced = [u for u in elig if mem[u] == "on"]
        free = [u for u in elig if mem[u] is None]
        target = target_count(len(elig), len(forced), pct)
        want = target - len(forced)

        mine = lambda: [u for u, a in self.store.active.items()
                        if a["group"] == gid and a["source"] == "percentage"]
        for uid in mine():
            if reroll or (uid not in free and uid not in forced):
                self.release_user(uid)
        self.load_users()                            # fresh values after the releases above
        for uid in forced:
            self.activate_user(uid, gid, "manual", exp)
        cur = mine()
        if len(cur) > want:
            for uid in random.sample(cur, len(cur) - want):
                self.release_user(uid)
        elif len(cur) < want:
            pool = [u for u in free if u not in self.store.active]
            for uid in random.sample(pool, min(len(pool), want - len(cur))):
                self.activate_user(uid, gid, "percentage", exp)
        for a in self.store.active.values():
            if a["group"] == gid:
                a["expires_at"] = exp
        active = sum(1 for a in self.store.active.values() if a["group"] == gid)
        return {"eligible": len(elig), "target": target, "active": active,
                "shortfall": max(target - active, 0)}

    def apply(self, reroll):
        gid = self.need_group()
        if not gid:
            return
        try:
            pct = int(self.pct.get())
        except (tk.TclError, ValueError):
            messagebox.showwarning("Percent", "Enter a whole number from 0 to 100.")
            return
        if not 0 <= pct <= 100:
            messagebox.showwarning("Percent", "Percent must be between 0 and 100.")
            return
        mins = self.minutes_value()
        timer = f"for {mins} minutes" if mins else "with no time limit"
        what = "Re-roll and activate" if reroll else "Activate"
        if not messagebox.askyesno("Confirm", f"{what} {self.preview.cget('text')}\n{timer}?"):
            return
        result = {}
        self.act(lambda: result.update(self.activate_group(gid, pct, mins, reroll)))
        if result.get("shortfall"):
            messagebox.showinfo("Not enough users",
                                f"Only {result['active']} of {result['target']} could be "
                                f"activated (others may be banned, forced OFF or already "
                                f"active in another group).")

    def stop_group(self):
        gid = self.need_group()
        if gid and messagebox.askyesno("Stop", "Switch every active user in this group off now?"):
            self.act(lambda: self.stop(gid))

    def set_override(self, mode):
        gid = self.need_group()
        sel = self.member_tree.selection()
        if not gid:
            return
        if not sel:
            messagebox.showinfo("Select a user", "Click a member in the list first.")
            return
        uid = sel[0]

        def go():
            self.load_users()
            g = self.store.group(gid)
            if mode == "on" and self.user_by_id[uid]["is_banned"]:
                raise RuntimeError("A banned user cannot be activated.")
            g["members"][uid] = mode
            if mode == "on":
                mins = self.minutes_value()
                self.activate_user(uid, gid, "manual", now_z(timedelta(minutes=mins)) if mins else None)
            elif mode == "off":
                self.release_user(uid)
            else:                                    # cleared: only undo a manual activation
                a = self.store.active.get(uid)
                if a and a["group"] == gid and a["source"] == "manual":
                    self.release_user(uid)
        self.act(go)


if __name__ == "__main__":
    App().mainloop()
