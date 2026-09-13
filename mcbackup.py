#!/usr/bin/env python3
"""mcbackup - versioned Minecraft server backups to Cloudflare R2 (restic).

Discovers MCSManager instances, snapshots them while players are active,
keeps pinned checkpoints, and restores any point in time.
"""
import argparse, datetime, fcntl, glob, json, os, re, shutil, socket, struct
import subprocess, sys, tempfile, time, tomllib, urllib.parse, urllib.request, zipfile

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE, "config.toml")
ENV_FILE = os.path.join(BASE, "env")
STATE_FILE = os.path.join(BASE, "state.json")
LOCK_FILE = os.path.join(BASE, ".lock")
LOG_FILE = os.path.join(BASE, "mcbackup.log")
WORLD_DIRS = ("playerdata", "region", "entities", "poi", "data", "stats", "advancements")


# ---------------------------------------------------------------- utilities
def log(msg, level="INFO"):
    line = f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} [{level}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_env():
    if not os.path.exists(ENV_FILE):
        return
    for raw in open(ENV_FILE):
        raw = raw.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        k, v = raw.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"'))


def load_config():
    with open(CONFIG_FILE, "rb") as f:
        return tomllib.load(f)


def load_state():
    try:
        return json.load(open(STATE_FILE))
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    json.dump(state, open(tmp, "w"), indent=2)
    os.replace(tmp, STATE_FILE)


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "unnamed"


def fmt_ts(ts):
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def ago(ts):
    if not ts:
        return "never"
    d = int(time.time() - ts)
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h{(d % 3600) // 60:02d}m ago"
    return f"{d // 86400}d ago"


class Lock:
    """Serialises restic operations between the daemon and manual CLI runs."""
    def __enter__(self):
        self.f = open(LOCK_FILE, "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.f, fcntl.LOCK_UN)
        self.f.close()


# ---------------------------------------------------------------- sources
class Source:
    def __init__(self, name, root, kind, mcsm_uuid=None, port=None, worlds=(), manual=False):
        self.name, self.root, self.kind = name, root, kind
        self.mcsm_uuid, self.port, self.worlds, self.manual = mcsm_uuid, port, list(worlds), manual

    @property
    def is_archive(self):
        return os.path.isfile(self.root)


def parse_properties(path):
    props = {}
    for line in open(path, errors="replace"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            props[k.strip()] = v.strip()
    return props


def find_server_root(cwd):
    """server.properties in cwd, or one level down (zip imports keep a top folder)."""
    if os.path.exists(os.path.join(cwd, "server.properties")):
        return cwd
    for d in sorted(glob.glob(os.path.join(cwd, "*/"))):
        if os.path.exists(os.path.join(d, "server.properties")):
            return d.rstrip("/")
    return None


def find_worlds(root):
    return sorted(os.path.dirname(p) for p in glob.glob(os.path.join(root, "*", "level.dat")))


def discover(cfg):
    sources = []
    m = cfg.get("mcsmanager", {})
    ignore = set(m.get("ignore", []))
    for path in sorted(glob.glob(os.path.join(m.get("instance_config_dir", ""), "*.json"))):
        try:
            d = json.load(open(path))
        except (OSError, ValueError):
            continue
        nick, uuid = d.get("nickname", ""), os.path.basename(path)[:-5]
        if nick in ignore or uuid in ignore or not d.get("cwd"):
            continue
        root = find_server_root(d["cwd"])
        if not root:
            continue
        props = parse_properties(os.path.join(root, "server.properties"))
        sources.append(Source(slugify(nick), root, "mcsmanager", uuid,
                              int(props.get("server-port", 25565)), find_worlds(root)))
    for s in cfg.get("sources", []):
        root = s["path"]
        worlds = [] if os.path.isfile(root) else find_worlds(find_server_root(root) or root)
        sources.append(Source(slugify(s["name"]), root, "manual", worlds=worlds,
                              manual=s.get("manual", True)))
    return sources


def get_source(cfg, name):
    for s in discover(cfg):
        if s.name == name:
            return s
    sys.exit(f"unknown source '{name}' (see: mcbackup discover)")


# ---------------------------------------------------------------- minecraft ping
def mc_ping(port, host="127.0.0.1", timeout=3):
    """Server-list ping. Returns online player count, or None if unreachable."""
    def varint(n):
        out = b""
        while True:
            b, n = n & 0x7F, n >> 7
            out += bytes([b | (0x80 if n else 0)])
            if not n:
                return out

    def pack(b):
        return varint(len(b)) + b

    def read_varint(s):
        n = shift = 0
        while True:
            b = s.recv(1)
            if not b:
                raise ConnectionError("eof")
            n |= (b[0] & 0x7F) << shift
            if not b[0] & 0x80:
                return n
            shift += 7

    try:
        with socket.create_connection((host, port), timeout) as s:
            hs = b"\x00" + varint(770) + pack(host.encode()) + struct.pack(">H", port) + b"\x01"
            s.sendall(pack(hs) + pack(b"\x00"))
            read_varint(s); read_varint(s)
            n = read_varint(s)
            buf = b""
            while len(buf) < n:
                chunk = s.recv(n - len(buf))
                if not chunk:
                    break
                buf += chunk
            return int(json.loads(buf)["players"]["online"])
    except (OSError, ValueError, KeyError):
        return None


# ---------------------------------------------------------------- MCSManager API
class MCSM:
    def __init__(self):
        self.url = os.environ.get("MCSM_URL", "http://127.0.0.1:23333").rstrip("/")
        self.key = os.environ.get("MCSM_APIKEY", "")
        self._daemon = None

    @property
    def enabled(self):
        return bool(self.key)

    def _call(self, method, path, params=None, body=None):
        params = dict(params or {}, apikey=self.key)
        req = urllib.request.Request(f"{self.url}{path}?{urllib.parse.urlencode(params)}",
                                     method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Content-Type": "application/json",
                                              "X-Requested-With": "XMLHttpRequest"})
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.load(r)
        if res.get("status") != 200:
            raise RuntimeError(f"MCSManager {path}: {res}")
        return res["data"]

    def daemon_id(self):
        if not self._daemon:
            self._daemon = self._call("GET", "/api/overview")["remote"][0]["uuid"]
        return self._daemon

    def status(self, uuid):
        """-1 busy, 0 stopped, 1 stopping, 2 starting, 3 running"""
        return self._call("GET", "/api/instance", {"uuid": uuid, "daemonId": self.daemon_id()})["status"]

    def command(self, uuid, cmd):
        self._call("POST", "/api/protected_instance/command",
                   {"uuid": uuid, "daemonId": self.daemon_id(), "command": cmd})


# ---------------------------------------------------------------- change detection
def changes_since(src, ts, excludes):
    """Count files modified after ts, grouped by area, plus newest mtime."""
    counts, newest = {"world": 0, "player": 0, "plugins": 0, "other": 0}, 0
    root = src.root
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        dirnames[:] = [d for d in dirnames if not excluded(os.path.join(rel, d), excludes)]
        for fn in filenames:
            p = os.path.join(rel, fn)
            if excluded(p, excludes):
                continue
            try:
                mt = os.stat(os.path.join(dirpath, fn)).st_mtime
            except OSError:
                continue
            if mt <= ts:
                continue
            newest = max(newest, mt)
            parts = p.split(os.sep)
            if "playerdata" in parts or "stats" in parts or "advancements" in parts or "userdata" in parts:
                counts["player"] += 1
            elif any(os.path.join(root, parts[0]) == w for w in src.worlds):
                counts["world"] += 1
            elif parts[0] == "plugins":
                counts["plugins"] += 1
            else:
                counts["other"] += 1
    return counts, newest


def excluded(relpath, excludes):
    import fnmatch
    relpath = relpath.replace("\\", "/").lstrip("./")
    base = os.path.basename(relpath)
    for pat in excludes:
        if "/" in pat:
            if fnmatch.fnmatch(relpath, pat) or fnmatch.fnmatch(relpath, pat + "/*"):
                return True
        elif fnmatch.fnmatch(base, pat):
            return True
    return False


# ---------------------------------------------------------------- restic
def restic(*args, capture=True, check=True):
    cmd = ["restic", "--no-cache" if os.environ.get("RESTIC_NO_CACHE") else "--quiet", *args]
    r = subprocess.run(cmd, capture_output=capture, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"restic {' '.join(args[:2])} failed: {(r.stderr or '').strip()[-800:]}")
    return r


def snapshots(host=None):
    args = ["snapshots", "--json"]
    if host:
        args += ["--host", host]
    out = restic(*args).stdout
    snaps = json.loads(out) if out.strip() else []
    for s in snaps:
        tags = dict(t.split(":", 1) for t in s.get("tags", []) if ":" in t)
        s["kind"] = tags.get("kind", "?")
        s["msg"] = tags.get("msg", "")
        s["players"] = tags.get("players", "")
        s["t"] = datetime.datetime.fromisoformat(s["time"][:19] + s["time"][19:].replace("Z", "+00:00")
                                                 if s["time"].endswith("Z") else s["time"])
    return sorted(snaps, key=lambda s: s["time"])


def resolve_snapshot(host, ref):
    snaps = snapshots(host)
    if not snaps:
        sys.exit(f"no snapshots for {host}")
    if ref in ("latest", None):
        return snaps[-1]
    for s in snaps:
        if s["id"].startswith(ref) or s["short_id"] == ref:
            return s
    try:  # date / datetime -> newest snapshot at or before it
        want = datetime.datetime.fromisoformat(ref)
        if want.tzinfo is None:
            want = want.astimezone()
        if len(ref) <= 10:  # bare date -> end of that day
            want = want.replace(hour=23, minute=59, second=59)
        before = [s for s in snaps if s["t"] <= want]
        if not before:
            sys.exit(f"no snapshot of {host} at or before {ref} (oldest: {snaps[0]['t']:%Y-%m-%d %H:%M})")
        return before[-1]
    except ValueError:
        sys.exit(f"cannot resolve snapshot '{ref}' (use an id, 'latest', or YYYY-MM-DD[THH:MM])")


def do_backup(cfg, src, kind="auto", msg="", players=None, reason=""):
    """Snapshot one source. Returns the new snapshot id."""
    excludes = cfg.get("exclude", [])
    mcsm = MCSM()
    flushed = False
    root = src.root
    tmpdir = None
    if src.is_archive:  # .zip archive -> extract to a temp dir first
        tmpdir = tempfile.mkdtemp(prefix=src.name + "-", dir=os.path.join(BASE, "tmp"))
        log(f"[{src.name}] extracting archive {root} -> {tmpdir}")
        with zipfile.ZipFile(root) as z:
            z.extractall(tmpdir)
        root = find_server_root(tmpdir) or tmpdir
    try:
        if src.mcsm_uuid and mcsm.enabled and cfg["policy"].get("flush_before_backup", True):
            try:
                if mcsm.status(src.mcsm_uuid) == 3:
                    mcsm.command(src.mcsm_uuid, "save-off")
                    mcsm.command(src.mcsm_uuid, "save-all flush")
                    flushed = True
                    time.sleep(cfg["policy"].get("flush_wait_seconds", 5))
            except Exception as e:  # noqa: BLE001
                log(f"[{src.name}] could not flush via MCSManager ({e}); backing up anyway", "WARN")
        tags = [f"kind:{kind}"]
        if msg:
            tags.append("msg:" + msg.replace(",", ";"))
        if players is not None:
            tags.append(f"players:{players}")
        if reason:
            tags.append("reason:" + reason.replace(",", ";"))
        args = ["backup", root, "--host", src.name, "--json", "--one-file-system", "--exclude-caches"]
        for t in tags:
            args += ["--tag", t]
        for e in excludes:
            args += ["--exclude", e]
        with Lock():
            r = restic(*args)
        summary = next((json.loads(l) for l in reversed(r.stdout.splitlines())
                        if l.startswith("{") and '"summary"' in l), {})
        sid = summary.get("snapshot_id", "?")[:8]
        log(f"[{src.name}] snapshot {sid} ({kind}{' - ' + msg if msg else ''}): "
            f"{summary.get('files_new', 0)} new, {summary.get('files_changed', 0)} changed files, "
            f"{summary.get('data_added', 0) / 1e6:.1f} MB uploaded, "
            f"{summary.get('total_duration', 0):.0f}s")
        return sid
    finally:
        if flushed:
            try:
                mcsm.command(src.mcsm_uuid, "save-on")
            except Exception as e:  # noqa: BLE001
                log(f"[{src.name}] FAILED to re-enable saving: {e} - run 'save-on' in console!", "ERROR")
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def do_prune(cfg):
    r = cfg.get("retention", {})
    args = ["forget", "--prune", "--group-by", "host",
            "--keep-tag", "kind:checkpoint", "--keep-tag", "kind:import"]
    for k in ("last", "hourly", "daily", "weekly", "monthly", "yearly"):
        if r.get(f"keep_{k}"):
            args += [f"--keep-{k}", str(r[f"keep_{k}"])]
    with Lock():
        restic(*args)
    log("prune complete")


# ---------------------------------------------------------------- daemon policy
def tick(cfg, state):
    pol = cfg["policy"]
    now = time.time()
    excludes = cfg.get("exclude", [])
    for src in discover(cfg):
        if src.manual:
            continue
        st = state.setdefault(src.name, {})
        online = mc_ping(src.port) if src.port else None
        counts, newest = changes_since(src, st.get("last_backup", 0), excludes)
        changed = sum(counts.values())
        if online:
            st["last_online"] = now
        if changed:
            st["last_activity"] = newest
        st["pending"] = counts
        since = now - st.get("last_backup", 0)
        reason = None
        if not changed:
            pass
        elif not st.get("last_backup"):
            reason = "initial"
        elif online and since >= pol["online_interval_min"] * 60:
            reason = f"{online} online"
        elif not online and (counts["player"] or counts["world"]) \
                and now - newest >= pol["after_session_min"] * 60 \
                and st.get("last_backup", 0) < newest:
            reason = "session ended"
        elif since >= pol["max_interval_hours"] * 3600:
            reason = "daily"
        if not reason:
            continue
        kind, msg = "auto", ""
        if now - st.get("last_checkpoint", 0) >= pol.get("auto_checkpoint_days", 7) * 86400:
            kind, msg = "checkpoint", "auto checkpoint"
        detail = ", ".join(f"{k} {v}" for k, v in counts.items() if v)
        try:
            do_backup(cfg, src, kind, msg, online, f"{reason} ({detail})")
            st["last_backup"] = time.time()
            st["last_reason"] = reason
            st["pending"] = {}
            if kind == "checkpoint":
                st["last_checkpoint"] = time.time()
        except Exception as e:  # noqa: BLE001
            log(f"[{src.name}] backup failed: {e}", "ERROR")
        save_state(state)
    if now - state.get("_last_prune", 0) >= 86400:
        try:
            do_prune(cfg)
            state["_last_prune"] = now
        except Exception as e:  # noqa: BLE001
            log(f"prune failed: {e}", "ERROR")
    save_state(state)


# ---------------------------------------------------------------- commands
def cmd_discover(cfg, a):
    for s in discover(cfg):
        worlds = ", ".join(os.path.basename(w) for w in s.worlds) or ("archive" if s.is_archive else "-")
        flag = " (manual only)" if s.manual else ""
        print(f"{s.name:<16} {s.kind:<11} port {s.port or '-':<6} worlds: {worlds:<30}{flag}\n"
              f"{'':16} {s.root}")


def cmd_status(cfg, a):
    state = load_state()
    print(f"{'source':<16}{'online':>7}  {'last snapshot':<20}{'pending changes':<44}last reason")
    for s in discover(cfg):
        st = state.get(s.name, {})
        online = mc_ping(s.port) if s.port and not s.manual else None
        pend = ", ".join(f"{k} {v}" for k, v in st.get("pending", {}).items() if v) or "-"
        print(f"{s.name:<16}{'down' if online is None else online:>7}  "
              f"{ago(st.get('last_backup')):<20}{pend:<44}{st.get('last_reason', '-')}")


def cmd_backup(cfg, a, kind="manual"):
    src = get_source(cfg, a.source)
    if src.is_archive and kind == "manual":
        kind = "import"  # archives are one-offs: pin them so retention never drops them
    sid = do_backup(cfg, src, kind, a.message or "", mc_ping(src.port) if src.port else None, "manual")
    state = load_state()
    st = state.setdefault(src.name, {})
    st["last_backup"] = time.time()
    st["pending"] = {}
    if kind == "checkpoint":
        st["last_checkpoint"] = time.time()
    save_state(state)
    print(f"snapshot {sid} created")


def cmd_import(cfg, a):
    src = Source(slugify(a.name), os.path.abspath(a.path), "import")
    if not os.path.exists(src.root):
        sys.exit(f"{src.root} does not exist")
    sid = do_backup(cfg, src, "import", a.message or f"import of {os.path.basename(src.root)}")
    print(f"imported as source '{src.name}', snapshot {sid}")


def cmd_history(cfg, a):
    snaps = snapshots(None if a.all else a.source)
    if not snaps:
        print("no snapshots yet")
        return
    print(f"{'id':<10}{'when':<18}{'source':<16}{'kind':<11}{'plyr':>4}  note")
    for s in snaps[-a.limit:]:
        mark = "*" if s["kind"] in ("checkpoint", "import") else " "
        note = s["msg"] or (dict(t.split(":", 1) for t in s.get("tags", []) if ":" in t).get("reason", ""))
        print(f"{s['short_id']:<10}{s['t'].astimezone():%Y-%m-%d %H:%M}  {s['hostname']:<16}"
              f"{mark + s['kind']:<11}{s['players']:>4}  {note}")
    print("\n* = pinned (never pruned)")


def cmd_diff(cfg, a):
    x, y = resolve_snapshot(a.source, a.a), resolve_snapshot(a.source, a.b)
    print(f"changes from {x['short_id']} ({x['t'].astimezone():%Y-%m-%d %H:%M}) "
          f"to {y['short_id']} ({y['t'].astimezone():%Y-%m-%d %H:%M}):")
    r = restic("diff", x["id"], y["id"], check=False)
    print(r.stdout or r.stderr)


def cmd_restore(cfg, a):
    src = get_source(cfg, a.source)
    snap = resolve_snapshot(src.name, a.snapshot)
    when = snap["t"].astimezone()
    snap_root = snap["paths"][0]
    if a.in_place:
        if src.is_archive:
            sys.exit("cannot restore in place over an archive")
        if src.port and mc_ping(src.port) is not None:
            sys.exit(f"{src.name} is running - stop it in MCSManager first")
        mcsm = MCSM()
        if src.mcsm_uuid and mcsm.enabled and mcsm.status(src.mcsm_uuid) != 0:
            sys.exit(f"{src.name} is not stopped according to MCSManager")
        target = tempfile.mkdtemp(prefix=f"restore-{src.name}-", dir=os.path.join(BASE, "tmp"))
    else:
        target = a.to or os.path.join(BASE, "restores", f"{src.name}-{when:%Y%m%d-%H%M}-{snap['short_id']}")
    print(f"restoring {src.name} @ {when:%Y-%m-%d %H:%M} (snapshot {snap['short_id']}) -> {target}")
    with Lock():
        restic("restore", snap["id"], "--target", target, capture=False)
    restored = os.path.join(target, snap_root.lstrip("/"))
    if not a.in_place:
        print(f"done. server files are in:\n  {restored}")
        return
    # safety copy of the current state, then sync (excluded paths are left untouched)
    safety = os.path.join(BASE, "restores", f"{src.name}-pre-restore-{datetime.datetime.now():%Y%m%d-%H%M}")
    paths = src.worlds if a.worlds_only else [src.root]
    for p in paths:
        rel = os.path.relpath(p, src.root)
        shutil.copytree(p, os.path.join(safety, rel), symlinks=True)
        rs = ["rsync", "-a", "--delete"] + [f"--exclude={e}" for e in cfg.get("exclude", [])]
        subprocess.run(rs + [os.path.join(restored, rel) + "/", p + "/"], check=True)
    shutil.rmtree(target, ignore_errors=True)
    print(f"done. {src.name} now matches {when:%Y-%m-%d %H:%M}. previous state saved to:\n  {safety}")


def cmd_daemon(cfg, a):
    log("daemon starting")
    state = load_state()
    while True:
        try:
            cfg = load_config()
            tick(cfg, state)
        except Exception as e:  # noqa: BLE001
            log(f"tick failed: {e}", "ERROR")
        time.sleep(cfg["policy"].get("tick_seconds", 300))


def cmd_init(cfg, a):
    r = restic("cat", "config", check=False)
    if r.returncode == 0:
        print("repository already initialised:", os.environ.get("RESTIC_REPOSITORY"))
        return
    restic("init", capture=False)
    print("repository initialised:", os.environ.get("RESTIC_REPOSITORY"))


def main():
    load_env()
    p = argparse.ArgumentParser(prog="mcbackup", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="initialise the restic repository on R2")
    sub.add_parser("discover", help="list detected instances and manual sources")
    sub.add_parser("status", help="online players, last snapshot, pending changes")
    for name, h in (("backup", "snapshot a source now"), ("checkpoint", "pinned snapshot (never pruned)")):
        q = sub.add_parser(name, help=h)
        q.add_argument("source")
        q.add_argument("-m", "--message", help="note shown in history")
    q = sub.add_parser("import", help="one-off backup of a directory or .zip archive")
    q.add_argument("path"); q.add_argument("--name", required=True); q.add_argument("-m", "--message")
    q = sub.add_parser("history", help="list snapshots")
    q.add_argument("source", nargs="?"); q.add_argument("--all", action="store_true")
    q.add_argument("-n", "--limit", type=int, default=40)
    q = sub.add_parser("diff", help="what changed between two snapshots")
    q.add_argument("source"); q.add_argument("a"); q.add_argument("b", nargs="?", default="latest")
    q = sub.add_parser("restore", help="restore a snapshot (id, 'latest', or a date)")
    q.add_argument("source"); q.add_argument("snapshot", nargs="?", default="latest")
    q.add_argument("--to", help="restore into this directory (default: /opt/mcbackup/restores/...)")
    q.add_argument("--in-place", action="store_true", help="overwrite the live instance (must be stopped)")
    q.add_argument("--worlds-only", action="store_true", help="with --in-place: only world folders")
    sub.add_parser("prune", help="apply retention policy now")
    sub.add_parser("daemon", help="run the scheduler loop")
    a = p.parse_args()
    if a.cmd != "init" and not os.environ.get("RESTIC_REPOSITORY"):
        sys.exit(f"RESTIC_REPOSITORY not set - fill in {ENV_FILE}")
    cfg = load_config()
    cmds = {"discover": cmd_discover, "status": cmd_status, "backup": cmd_backup,
            "checkpoint": lambda c, x: cmd_backup(c, x, "checkpoint"), "import": cmd_import,
            "history": cmd_history, "diff": cmd_diff, "restore": cmd_restore,
            "prune": lambda c, x: do_prune(c), "daemon": cmd_daemon, "init": cmd_init}
    if a.cmd == "history" and not a.source:
        a.all = True
    cmds[a.cmd](cfg, a)


if __name__ == "__main__":
    main()
