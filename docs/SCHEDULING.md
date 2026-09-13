# Scheduling and snapshot policy

The daemon (`mcbackup daemon`, run by `mcbackup.service`) wakes up every `tick_seconds` and,
for each non-manual source, decides whether a snapshot is worth taking. Nothing is uploaded
unless something changed.

## What a tick looks at

1. **Players online** — a Minecraft server-list ping to `127.0.0.1:<server-port>` (port read from
   `server.properties`). No plugin or RCON needed; status pings are not logged by the server.
   `None` means the server is down or not answering.
2. **Changed files** — walks the instance directory (skipping excludes) and counts files whose
   mtime is newer than the last snapshot, bucketed as:
   - `player` — anything under `playerdata/` (or `players/` on 26.x worlds), `stats/`, `advancements/` or a plugin `userdata/` dir
   - `world` — anything else inside a world folder (region, entities, poi, level.dat…)
   - `plugins` — anything under `plugins/`
   - `other` — everything else (server configs, jars…)

   It also records the newest mtime seen, used for the "session ended" rule.

## Decision rules

Evaluated in order; the first match wins. `since` = time since the last snapshot of this source.

| rule | condition | note tag |
|---|---|---|
| nothing changed | no files newer than last snapshot | — (skip) |
| initial | never backed up | `initial` |
| online | players > 0 and `since ≥ online_interval_min` | `3 online` |
| session ended | players = 0, player or world files changed, newest change ≥ `after_session_min` ago | `session ended` |
| daily | `since ≥ max_interval_hours` | `daily` |

The note is stored on the snapshot as a `reason:` tag together with the change counts, and
shown by `mcbackup history`, e.g. `session ended (world 88, player 3)`.

Why these rules:

- **online** gives you regular points in time during a long session, so a griefing incident or
  a bad TNT accident can be rolled back to minutes before it happened.
- **session ended** guarantees the final state of every session is captured, even a five-minute
  one that fell between two `online` intervals.
- **daily** catches changes that don't come from play — plugin updates, config edits.

## Checkpoints

A checkpoint is an ordinary snapshot tagged `kind:checkpoint`. Retention never removes it.

- `mcbackup checkpoint <source> -m "message"` creates one on demand.
- The daemon promotes the first snapshot taken more than `auto_checkpoint_days` after the previous
  checkpoint (message `auto checkpoint`).
- Archive imports (`mcbackup import`, or `mcbackup backup` of a `.zip` source) are tagged
  `kind:import` and pinned the same way.

Because restic deduplicates at the chunk level, a checkpoint costs nothing extra to keep — it
just prevents the chunks it references from being garbage-collected.

## Retention

Once a day the daemon runs the equivalent of

```
restic forget --prune --group-by host \
  --keep-tag kind:checkpoint --keep-tag kind:import \
  --keep-last 10 --keep-hourly 48 --keep-daily 30 --keep-weekly 26 --keep-monthly 36
```

Grouping by host means each source gets its own hourly/daily/weekly/monthly buckets.
`--prune` then deletes chunks no snapshot references any more, which is where the space actually
comes back. Prune can take a few minutes on a large repository; it holds the repository lock, so a
snapshot that lands at the same time simply waits (see [INTERNALS.md](INTERNALS.md#locking)).

## Consistency: the flush step

A Minecraft server writes region files continuously. Copying one mid-write can leave a chunk
half-updated. With `MCSM_APIKEY` set and `flush_before_backup = true`, each snapshot of a running
instance is bracketed by console commands sent through the MCSManager panel API:

```
save-off            # stop the autosave from writing while we read
save-all flush      # write everything in memory to disk, synchronously
   … wait flush_wait_seconds …
   … restic backup …
save-on             # resume autosaves (always sent, even if the backup failed)
```

You'll see `Automatic saving is now disabled` / `Saved the game` / `Automatic saving is now
enabled` in the server log around every snapshot. Players are not affected. If the API call fails
the backup proceeds without the flush and a `WARN` line is logged.

## State

`/opt/mcbackup/state.json` remembers, per source, the last snapshot time, last checkpoint time, last
activity, the reason for the last snapshot and the pending change counts shown by `mcbackup status`.
Deleting it is safe: every source is simply treated as never backed up, which triggers one `initial`
snapshot (cheap — restic dedups against what's already in the repository).
