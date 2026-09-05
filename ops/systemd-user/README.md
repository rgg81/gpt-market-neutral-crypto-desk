# Optional systemd user timers

These are unprivileged templates, not an installer. Replace `@DESK_ROOT@` with the absolute desk
path and `@BINANCE_PROXY_ROOT@` with the exact local proxy project path, then copy the rendered
files to `~/.config/systemd/user/` only if the operator deliberately chooses systemd instead of
cron. Never enable cron and these timers as competing schedulers. Enable `binance-proxy.service`
with the timers; keeping the proxy in its own service prevents systemd from killing a proxy child
when a cycle oneshot exits. The cycle template sets `BINANCE_PROXY_EXTERNAL_MANAGER=systemd`, so
the launcher waits on the exact read-only identity probe and never kills or replaces the
systemd-owned process. The normal cron launcher leaves that variable unset and retains its narrow
self-healing owner behavior.

The task timers poll at :07/:17/:27/:37/:47/:57. Their UTC claim gate still admits only 00:07 for
the daily GPT cycle and 08:07/16:07 for token-free heartbeats; polling supplies bounded retry after
wake-up or transient preflight failure. `Persistent=true`, the claim gate, and the shared `flock`
remain authoritative against duplicates. Health is offset to :02/:12/:22/:32/:42/:52 and takes a
consistent state-transaction snapshot. It writes only local, deduplicated alerts. Add
`--alert-command '/absolute/program arg'` to the rendered health service only when an operator
explicitly wants a local command or webhook client invoked.

For two-host availability, use one active writer and one read-only standby. Replicate a
filesystem-consistent snapshot containing the entire repository, `live_state/`, `live_memory/`,
`logs/desk-schedule-claims.json`, and reflector trust anchors. Never point two active hosts at a
shared state directory. Fail over only after proving the old host is fenced, recovering both
transaction files, and running `scripts/desk_health.py --strict` on the promoted host.
