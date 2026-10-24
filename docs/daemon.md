# mcbd: the user-space daemon

`daemon/mcbd.c`, packaged as `br2-external/package/mcbd`, started at boot by
`/etc/init.d/S50mcbd`. It reads the `mcb_bridge` driver through sysfs and logs

- every sample to `/var/log/mcbd/samples.csv` (tmpfs), rotated once to
  `samples.csv.1` at 1 MiB (~2 min at 200 Hz, ~8 min at 50 Hz);
- events to syslog (`daemon` facility, `/var/log/messages`): device found or
  gone, link up/down, sample stale/fresh, peer reboot, and every 10 s the error
  counters that moved.

## Driver interface

Under the bound device (`/sys/bus/serial/drivers/mcb_bridge/<dev>/`, found by
glob, not a fixed `serial0-0`):

| Attribute | Content | Notified |
|-----------|---------|----------|
| `sample`  | `boot_id seq timestamp_us rx_ns count v0 v1 ...` | each DATA frame |
| `link_up` | `1` / `0` | both edges |
| `rate`, counters | as before | no |

- `sample` is one snapshot taken under the driver's lock. Reading `channels`,
  `sample_seq` and `sample_timestamp_us` one after another can mix two frames.
- `rx_ns` is `ktime_get_ns()` when the frame was accepted, which is
  `CLOCK_MONOTONIC` in user space. `timestamp_us` is the ESP32's own clock and
  cannot be compared to Linux time.
- `count` is the number of DATA frames accepted since probe. It resets on
  module reload.
- `link_up` goes to `0` after 3 s without a valid frame. A delayed work item,
  pushed back by every frame, does the down-edge notify (a work item because
  `sysfs_notify()` can sleep). Without it a reader blocked in `poll()` would
  never hear about a silent link.

## Stale samples: mechanism in the driver, policy in the daemon

The driver keeps the last sample after the link goes quiet (it is still the
last true measurement), but never presents it without its receive time. The
daemon decides what is too old:

    stale_after = max(3 / rate_hz, 100 ms)      (1 s while the rate is unknown)

so at 50 Hz a stream stall is reported after ~100 ms, well before the driver's
3 s link timeout. On an unplug the log therefore reads: `sample stale`, then
`link down` ~3 s later, then after the replug `link up`, `peer rebooted`
(boot_id changed) and `samples fresh again`. A stale sample is never written
to the CSV as a new row; rows only appear for new samples, each with its age
at the time it was read (`age_us`).

The rate comes from the driver's `rate` attribute. It is re-read before a
sample is declared stale, so a lowered rate never produces false stale events.

## Losing samples: wire vs reader

sysfs is a mailbox holding the latest value, not a queue: notifications
coalesce, and a reader that falls behind sees only the newest sample. The
two kinds of loss are kept apart:

- `dropped_frames` (driver): frames lost on the wire, from `seq` gaps;
- `missed_by_reader` (daemon): samples the driver accepted but the daemon never
  read, from `count` gaps. `seq` would not do here: it also counts PONG/ACK
  frames and wire losses.

`rows + missed_by_reader` equals the driver's `count` delta exactly
(`tests/test_daemon.py` checks this with the daemon stopped for 1 s).
If every sample must reach user space, sysfs is the wrong interface: that
needs a buffered one (a kfifo-backed character device, or the IIO buffer
planned for later).

## Event loop

One thread, one `poll()` over `sample`, `link_up` (`POLLPRI`) and a
`signalfd`, with a timeout to the nearest deadline (stale check, counter
report, or the 1 s device search while none is bound).

- A sysfs file must be read once after `open()` before `poll()` can wake on it;
  after each wakeup it is re-read from offset 0 with `pread()`.
- A removed attribute reports `POLLERR|POLLPRI` forever and its reads fail with
  `ENODEV`: the daemon closes the files and searches again, so `rmmod` /
  `modprobe` and a boot without the DT node need no restart.
- Signals: `SIGTERM`/`SIGINT` exit, `SIGHUP` reopens the CSV
  (`S50mcbd reload`), `SIGUSR1` logs a status line.

mcbd never forks; `start-stop-daemon` backgrounds it and owns the pidfile.
There is no respawn: every expected fault is handled inside the loop, so an
exit is a bug.

Cost: 18 KB binary; boot to login is unchanged (~1.1 s under TCG).

## Future work

- Change-only logging: touch data barely changes while untouched, so writing a
  row only when the values change (plus a periodic heartbeat row) would shrink
  the CSV by orders of magnitude. Every row is logged today because it keeps
  the seq-gap and `missed_by_reader` checks trivial.
- A buffered driver interface (IIO) for lossless capture at high rates.
