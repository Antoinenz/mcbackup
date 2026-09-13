# Internals

One Python file, standard library only (3.11+ for `tomllib`), shelling out to `restic` and `rsync`.

```
mcbackup.py
├── utilities        logging, env/config/state loading, slugify, Lock
├── sources          Source class, MCSManager discovery, manual sources
├── mc_ping          server-list ping (player count)
├── MCSM             tiny MCSManager panel API client
├── change detection changes_since(), excluded()
├── restic           wrappers: restic(), snapshots(), resolve_snapshot(), do_backup(), do_prune()
├── tick()           the scheduler policy
└── cmd_*            CLI commands
```

## Discovery

`discover()` builds the list of sources every time it's called (cheap, and it means instances
created in MCSManager show up without a restart):

1. Read every `<instance_config_dir>/*.json`. Skip anything in `ignore` or without a `cwd`.
2. Find the server root: `server.properties` in `cwd`, else in any immediate subdirectory
   (zip imports frequently leave the server one level down; MCSManager's own start command
   wouldn't work there, but the backup still should).
3. Parse `server-port` from `server.properties`; find worlds as every immediate subdirectory
   containing `level.dat` (this catches `world_nether`, `world_the_end`, BSkyBlock worlds, Multiverse
   worlds…).
4. Append `[[sources]]` entries from config.

The source **slug** is derived from the MCSManager nickname (or `name` in config) and becomes the
restic `--host`. Renaming an instance therefore starts a new history; the old snapshots remain
under the old slug.

## Player detection

`mc_ping()` implements the modern server-list ping by hand: a handshake packet (protocol 770,
next-state 1) followed by a status request, then parses `players.online` out of the JSON reply.
Works with vanilla, Paper/Purpur/Pufferfish, Fabric and Forge; Bedrock players joining through
Geyser are counted because they are Java players from the server's point of view. Velocity/
BungeeCord front-ends aren't pinged — mcbackup targets the backend instance directly.

## MCSManager API

Three endpoints of the **panel** (not the daemon), authenticated with `?apikey=` and the
`X-Requested-With: XMLHttpRequest` header MCSManager requires:

| endpoint | use |
|---|---|
| `GET /api/overview` | first remote daemon's uuid (`daemonId`, cached per process) |
| `GET /api/instance?uuid&daemonId` | `status`: -1 busy, 0 stopped, 1 stopping, 2 starting, 3 running |
| `POST /api/protected_instance/command?uuid&daemonId&command` | console commands for the flush |

Only instances with `status == 3` get the flush; a stopped instance is backed up as-is.

## Snapshot mapping

| restic field | mcbackup meaning |
|---|---|
| `hostname` | source slug (`--host`) |
| `paths[0]` | instance root at the time (used to locate files inside a restored tree) |
| tag `kind:` | `auto`, `manual`, `checkpoint`, `import` |
| tag `msg:` | your `-m` message (commas replaced by `;` — restic uses commas to separate tags) |
| tag `players:` | player count at snapshot time |
| tag `reason:` | scheduler reason + change counts |

Retention keeps everything tagged `kind:checkpoint` or `kind:import`.

`restic backup` is run with `--json --one-file-system --exclude-caches` plus the configured
excludes; the final `summary` line is parsed for the snapshot id and upload statistics that go
into the log.

## Change detection

`changes_since(source, ts)` does an `os.walk` of the instance root, pruning excluded directories,
and counts files with `st_mtime > ts` into the four buckets described in
[SCHEDULING.md](SCHEDULING.md). It's mtime-only — no hashing — so a tick costs one directory walk,
roughly 2000–5000 stat calls for a typical instance. Restic does its own content comparison
during backup, so a touched-but-identical file never uploads anything.

## Locking

restic itself refuses concurrent writers with a repository lock, but that would surface as an
error mid-command. `Lock` (an `flock` on `/opt/mcbackup/.lock`) serialises restic invocations
between the daemon and any `mcbackup` command you run by hand, so a manual checkpoint during a
scheduled snapshot simply waits its turn.

## Failure handling

- A failing backup logs `ERROR` and the source is retried next tick; `last_backup` is not
  advanced, so the change set that triggered it is still pending.
- `save-on` is sent in a `finally`, so a crash between `save-off` and the end of the backup still
  re-enables autosaves. If even that fails, an `ERROR` line says to run `save-on` in the console.
- Prune failures don't block snapshots; they're retried a day later.
- The daemon loop catches everything per tick; a bad config edit produces one `ERROR` line per
  tick until it's fixed.

Logs go to stdout (journald: `journalctl -u mcbackup -f`) and `/opt/mcbackup/mcbackup.log`.

## Files on disk

```
/opt/mcbackup/
├── mcbackup.py
├── config.toml
├── env                 secrets, chmod 600
├── state.json          per-source timestamps and pending counts
├── mcbackup.log
├── .lock
├── restores/           restore targets and pre-restore safety copies
└── tmp/                archive extraction and in-place restore staging
/usr/local/bin/mcbackup             wrapper: re-execs itself with sudo, runs mcbackup.py
/etc/systemd/system/mcbackup.service
```

The service runs as root because MCSManager's instance files are root-owned. It's niced and uses
the idle I/O scheduling class so a snapshot doesn't compete with a running server.
