<div align="center">

# mcbackup

**Versioned, deduplicated backups of Minecraft servers to Cloudflare R2, with player-aware scheduling, pinned checkpoints and point-in-time restore.**

Auto-discovers every [MCSManager](https://mcsmanager.com) instance on the host and snapshots it while people are actually playing — every 30 minutes during a session, once more when the last player leaves, and never when nothing changed. Each snapshot is a [restic](https://restic.net) snapshot: encrypted, chunk-deduplicated, only the changes go up. Pin the moments that matter as checkpoints, and rewind any world to any day.

</div>

## Features

- **Zero per-server setup** — reads MCSManager's instance configs, finds `server.properties` and every world folder on its own; new instances are picked up automatically
- **Player-aware scheduling** — pings each server for its player count and watches file changes, so snapshots land during and right after sessions instead of on a dumb timer
- **Nothing changed, nothing uploaded** — a quiet server costs zero bandwidth and zero storage
- **Consistent snapshots** — `save-off` / `save-all flush` through the MCSManager API before reading, `save-on` after, so region files are never captured mid-write
- **Checkpoints** — pin a snapshot forever with a message; one is promoted automatically every week
- **Time travel** — list the history of a world, diff two points in time, restore by date into a folder or rewind the live instance in place
- **Retention that thins, not deletes** — hourly → daily → weekly → monthly, checkpoints exempt
- **Archives too** — one-off imports of old server zips, kept forever
- **Plain restic underneath** — any S3-compatible bucket works, and the standard `restic` tools work on the repository
- **Small** — one Python file, no dependencies beyond `restic` and `rsync`

## Installation

```sh
git clone https://github.com/Antoinenz/mcbackup && cd mcbackup
sudo ./install.sh
```

Linux with systemd and Python 3.11+. The installer puts everything in `/opt/mcbackup`, adds the `mcbackup` command and a systemd service, and installs `restic` and `rsync` if they're missing.

## Setup

1. Create an R2 bucket (any S3-compatible store works) and an API token with **Object Read & Write** on it
2. Edit `/opt/mcbackup/env` — the repository URL, the two keys, and your MCSManager API key (panel → user menu → API key)
3. **Copy `RESTIC_PASSWORD` somewhere safe.** It was generated for you and encrypts every backup; without it they're unreadable
4. Initialise and start:

```sh
mcbackup init
mcbackup discover                      # check what was found
sudo systemctl enable --now mcbackup
```

See [docs/CONFIGURATION.md](docs/CONFIGURATION.md) for every key in `env` and `config.toml`.

## Usage

```
mcbackup status                              online players, last snapshot, pending changes
mcbackup history [source]                    timeline of snapshots (* = pinned)
mcbackup checkpoint <source> -m "note"       pinned snapshot, never pruned
mcbackup backup <source> [-m note]           snapshot now
mcbackup diff <source> <a> [b]               what changed between two points in time
mcbackup restore <source> [snapshot]         unpack a snapshot into a folder
mcbackup restore <source> <snapshot> --in-place [--worlds-only]
                                             rewind the live instance (must be stopped)
mcbackup import <path> --name <name>         one-off backup of a directory or .zip
mcbackup prune                               apply retention now
```

`<snapshot>` is an id, `latest`, or a date — `2026-09-01` means "the newest snapshot from that day or earlier".

```
$ mcbackup history labubu-smp
id        when              source          kind       plyr  note
eda948de  2026-09-13 15:40  labubu-smp      *checkpoint   0  auto checkpoint
3c1f0a92  2026-09-14 20:30  labubu-smp       auto         3  3 online (world 412, player 6)
b7e20c11  2026-09-14 22:41  labubu-smp       auto         0  session ended (world 88, player 3)
bb093178  2026-09-15 09:02  labubu-smp      *checkpoint      castle finished

$ mcbackup restore labubu-smp 2026-09-14 --in-place --worlds-only
```

## How it decides when to back up

Every 5 minutes, per instance:

| when | then |
|---|---|
| players online, 30 min since last snapshot | snapshot |
| nobody online, world/player files changed, 10 min since the last change | snapshot (end of session) |
| anything changed, 24 h since last snapshot | snapshot |
| nothing changed | skip |

All intervals are configurable. Full rules, checkpoint promotion and retention in [docs/SCHEDULING.md](docs/SCHEDULING.md).

## Documentation

| | |
|---|---|
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | every `env` variable and `config.toml` key, exclude patterns, manual sources |
| [docs/SCHEDULING.md](docs/SCHEDULING.md) | the decision rules, checkpoints, retention, the flush step |
| [docs/RESTORE.md](docs/RESTORE.md) | history, diff, folder and in-place restores, recovering onto a new machine, using restic directly |
| [docs/INTERNALS.md](docs/INTERNALS.md) | discovery, the ping, MCSManager API calls, snapshot tagging, locking, failure handling |

## License

[MIT](LICENSE)
