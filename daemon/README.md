# The pinned, auto-healing fleet

Three layers, each watching the one below:

```
cloud Routine  ──health-pings──▶  maestro doctor   (alerts if heartbeat is stale)
launchd        ──every 5 min──▶   maestro dispatch  (the durable clock; survives reboot)
maestro dispatch ──spawns──▶      claude --bg reconcile-<KEY>   (the per-ticket workers)
```

## 1. launchd — the durable clock

`maestro dispatch` is stateless and fast: one sweep, then exit. launchd's
`StartInterval` fires it every 5 minutes, `RunAtLoad` runs it at login, and there is
no long-lived process to crash. This is what makes the fleet survive a reboot.

```bash
pip install -e .            # puts `maestro` on PATH
maestro init                # scaffold ~/.maestro + config.toml
daemon/install.sh up        # load the LaunchAgent (use --interval N to change cadence)
maestro doctor              # confirm a heartbeat appears
daemon/install.sh down      # uninstall
```

## 2. The cloud routine — watching the watcher

launchd is the single point of failure for "does the fleet survive a reboot." Guard it
with a cloud routine (Claude Code *Routine*, min 1-hour cadence) that runs `maestro
doctor` and alerts you if `stale: true` (no dispatch in >30 min) or if dead-letters pile
up. That alert is the only thing that tells you the fleet has silently stopped.

## 3. Cost control

12 concurrent Sonnet+Opus reconcilers multiply spend. Set `daily_token_ceiling` in
`config.toml`; `maestro doctor` surfaces it. The dispatcher needs no model at all, so the
*idle* fleet costs nothing — you only pay when tickets are actually moving.
