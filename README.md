# status-controller

Small desktop tool (Python + tkinter) for MarryMatch admins.

Every status change is a **plain update in Supabase** (`profiles.last_active_at`, `user_status.is_banned`),
exactly like editing the row by hand. This tool creates nothing in your database.

**Users tab** - see who is active now, search users, set a user online/offline, ban/unban.

**Groups tab** - organise users into groups and control their activity:
- **Percentage activation** - set e.g. 10% and exactly that share of the group's eligible members
  becomes active (rounded half up, 10% of 25 = 3). The random pick stays until you press **Re-roll**.
- **Manual override** - force a single user ON or OFF. Forced-ON members count toward the percentage;
  forced-OFF members are never picked. Banned users are never picked.
- **Auto-off timer** - minutes (default 20, `0` = no limit). When it runs out, the user's previous
  activity time is put back - unless they were genuinely active in the meantime (then it is kept).

Groups and running timers are saved on your computer in `~/.marrymatch_status_groups.json`.
Timers are checked by this app: they switch users off while it is open, and as soon as you open it again
if one ran out while it was closed. (Close the app with users still active and they stay active until
you open it again.)

## Run
```
python user_status_app.py
```
Paste your Supabase **service_role / secret key** in the app (kept in memory only, never saved).
