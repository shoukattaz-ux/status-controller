"""MarryMatch - User Status desktop tool.

Shows which users are active right now and lets you change their status.
Needs only Python 3 (tkinter ships with the normal python.org installers).

Run:  python user_status_app.py
Key:  paste your Supabase *service_role* key in the app (Supabase dashboard ->
      Project Settings -> API Keys), or set env var SUPABASE_SERVICE_KEY.
      The key is kept in memory only and is never saved to disk.
"""
import json
import os
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from tkinter import messagebox, ttk

DEFAULT_URL = "https://zaixmvhfblgarpjckjhc.supabase.co"
ONLINE_WINDOW_MIN = 5      # active within this many minutes => "Online"
AUTO_REFRESH_MS = 10_000


class Supabase:
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

    def set_last_active(self, uid, when):
        self._call("PATCH", f"profiles?id=eq.{uid}",
                   {"last_active_at": when.isoformat()})

    def set_banned(self, uid, value):
        self._call("PATCH", f"user_status?user_id=eq.{uid}", {"is_banned": value})


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


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MarryMatch - User Status")
        self.geometry("820x460")
        self.api = None
        self.rows = {}
        self.users = []

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

        sb = ttk.Frame(self, padding=(8, 0))
        sb.pack(fill="x")
        ttk.Label(sb, text="Search").pack(side="left")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.render())
        ent = ttk.Entry(sb, textvariable=self.query)
        ent.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(sb, text="Clear", command=lambda: self.query.set("")).pack(side="left")
        ttk.Label(sb, text="(name, id, online / offline / banned)").pack(side="left", padx=6)

        cols = ("name", "status", "last", "hidden", "banned")
        self.tree = ttk.Treeview(self, columns=cols, show="headings",
                                 selectmode="browse")
        for c, t, w in (("name", "User", 200), ("status", "Status", 110),
                        ("last", "Last active", 140), ("hidden", "Hides activity", 110),
                        ("banned", "Banned", 80)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.tag_configure("online", background="#d7f5dc")
        self.tree.tag_configure("banned", background="#f8d7da")
        self.tree.pack(fill="both", expand=True, padx=8)

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        ttk.Button(bar, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(bar, text="Set Online", command=self.set_online).pack(side="left", padx=6)
        ttk.Button(bar, text="Set Offline", command=self.set_offline).pack(side="left")
        ttk.Button(bar, text="Ban / Unban", command=self.toggle_ban).pack(side="left", padx=6)
        self.msg = ttk.Label(bar, text="Enter your service key and press Connect.")
        self.msg.pack(side="right")

        if self.key.get():
            self.connect()

    def connect(self):
        if not self.key.get().strip():
            messagebox.showwarning("Key needed", "Paste your Supabase service_role key.")
            return
        self.api = Supabase(self.url.get().strip(), self.key.get().strip())
        self.refresh(loop=True)

    def refresh(self, loop=False):
        if not self.api:
            return
        try:
            users = self.api.users()
        except Exception as e:
            self.msg.config(text="Error - see popup")
            messagebox.showerror("Could not load users", str(e))
            return
        self.users = users
        self.render()
        if loop:
            self.after(AUTO_REFRESH_MS, lambda: self.refresh(loop=True))

    def render(self):
        now = datetime.now(timezone.utc)
        q = self.query.get().strip().lower()
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        online = shown = 0
        for u in self.users:
            d = now - parse_ts(u["last_active_at"])
            is_on = d < timedelta(minutes=ONLINE_WINDOW_MIN) and not u["is_banned"]
            online += is_on
            state = "banned" if u["is_banned"] else ("online" if is_on else "offline")
            if q and not (q in u["display_name"].lower() or q in u["id"].lower()
                          or q == state):
                continue
            shown += 1
            self.rows[u["id"]] = u
            tags = ("banned",) if u["is_banned"] else (("online",) if is_on else ())
            self.tree.insert("", "end", iid=u["id"], tags=tags, values=(
                u["display_name"], "ONLINE" if is_on else "offline", ago(d),
                "yes" if u["hide_activity"] else "no",
                "YES" if u["is_banned"] else "no"))
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        self.msg.config(text=f"{online} online / {len(self.users)} users"
                             f"{f' - showing {shown}' if q else ''}")

    def selected(self):
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
        self.refresh()

    def set_online(self):
        u = self.selected()
        if u:
            self.act(lambda: self.api.set_last_active(u["id"], datetime.now(timezone.utc)))

    def set_offline(self):
        u = self.selected()
        if u:
            self.act(lambda: self.api.set_last_active(
                u["id"], datetime.now(timezone.utc) - timedelta(days=1)))

    def toggle_ban(self):
        u = self.selected()
        if not u:
            return
        new = not u["is_banned"]
        if messagebox.askyesno("Confirm", f"{'Ban' if new else 'Unban'} {u['display_name']}?"):
            self.act(lambda: self.api.set_banned(u["id"], new))


if __name__ == "__main__":
    App().mainloop()
