# History, diff and restore

## Naming a point in time

Every command that takes a `<snapshot>` accepts:

| form | meaning |
|---|---|
| `latest` (default) | newest snapshot of that source |
| `3c1f0a92` | a snapshot id (short or full, from `mcbackup history`) |
| `2026-09-01` | the newest snapshot taken **on or before the end of that day** |
| `2026-09-01T14:00` | the newest snapshot at or before that minute (local time) |

Dates resolve against the source's own snapshots, so "what did the world look like on the 1st" is
`mcbackup restore labubu-smp 2026-09-01` with no need to look up ids.

## Browsing history

```
mcbackup history                    # every source, newest last
mcbackup history labubu-smp -n 100  # one source
```

```
id        when              source          kind       plyr  note
eda948de  2026-09-13 15:40  labubu-smp      *checkpoint   0  auto checkpoint
3c1f0a92  2026-09-14 20:30  labubu-smp       auto         3  3 online (world 412, player 6)
b7e20c11  2026-09-14 22:41  labubu-smp       auto         0  session ended (world 88, player 3)
bb093178  2026-09-15 09:02  labubu-smp      *checkpoint      castle finished
```

`*` marks pinned snapshots. `plyr` is the player count at the time. `note` is your message for
manual snapshots and checkpoints, or the scheduler's reason for automatic ones.

## What changed?

```
mcbackup diff labubu-smp 2026-09-14 latest
mcbackup diff labubu-smp 3c1f0a92 b7e20c11
```

Prints restic's file-level diff: which region files, player files and plugin files were added,
removed or modified between the two points, plus byte totals. Handy for answering "did anything
happen on the server while I was away" or for finding which snapshot a change first appeared in.

## Restore into a folder

```
mcbackup restore labubu-smp 2026-09-14
mcbackup restore labubu-smp b7e20c11 --to /mnt/scratch/labubu-sept
```

The snapshot is unpacked under `/opt/mcbackup/restores/<source>-<time>-<id>/` (or `--to`), mirroring
the original absolute path beneath it. Nothing on the live server is touched. From there you can:

- copy a single `region/r.0.0.mca` back to fix one corrupted area
- copy `playerdata/<uuid>.dat` to give someone their inventory back
- drag the whole `world/` folder into a fresh instance to fork the world
- open it with any NBT tool

Restored folders are ordinary files; delete them when you're done.

## Restore in place (rewind the live instance)

```
mcbackup restore labubu-smp 2026-09-14T20:30 --in-place
mcbackup restore labubu-smp 2026-09-14T20:30 --in-place --worlds-only
```

Rewrites the instance directory to match the snapshot. Safeguards:

1. Refuses if the server answers a ping, or MCSManager reports it as anything but stopped.
   **Stop the instance in MCSManager first.**
2. Copies the current instance (or just the world folders with `--worlds-only`) to
   `/opt/mcbackup/restores/<source>-pre-restore-<time>/` before touching anything.
3. Syncs the snapshot over the live directory with `rsync --delete`, using the configured
   `exclude` list — so `logs/`, `libraries/`, archives etc. that were never backed up are left alone
   rather than deleted.

`--worlds-only` is the common case: rewind the world but keep the current plugins, their configs
and their databases (LuckPerms, Essentials userdata, economy). Without it, plugin data is rewound
too, which is what you want after a plugin update went badly.

Start the instance again from MCSManager afterwards. The daemon sees the changed files and takes
a fresh snapshot on its next cycle, so the rewind itself becomes part of the history.

## Recovering onto a new machine

The repository on R2 is self-contained. On a fresh host:

```sh
git clone https://github.com/Antoinenz/mcbackup && cd mcbackup && sudo ./install.sh
# fill /opt/mcbackup/env with the SAME repository URL, keys and RESTIC_PASSWORD
mcbackup history --all
mcbackup restore labubu-smp latest --to /tmp/labubu
```

Then create the instance in MCSManager and move the restored files into its directory (one
level up if they land in a nested folder — MCSManager runs the start command from the instance
root, so `server.properties` must sit there). Once the instance exists, `mcbackup discover` will
pick it up and, because restic identifies snapshots by hostname (the source slug), the new
snapshots continue the same history as long as the nickname produces the same slug.

## Using restic directly

The repository is plain restic. For anything mcbackup doesn't expose:

```sh
set -a; . /opt/mcbackup/env; set +a
restic snapshots --host labubu-smp
restic ls latest --host labubu-smp /opt/mcsmanager/daemon/data/InstanceData/<uuid>/world/playerdata
restic mount /mnt/restic        # browse every snapshot as a filesystem (needs fuse)
restic check                    # verify repository integrity
restic stats --mode raw-data    # actual space used in the bucket
```

Don't run `restic forget`/`prune` by hand without the `--keep-tag` flags mcbackup uses, or you'll
drop the checkpoints.
