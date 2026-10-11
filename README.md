**English** | [简体中文](README.zh-CN.md)

# mihomo-traffic-logger

Records how much traffic your Mihomo (Clash.Meta) core moves each day, so you can
review past days after the core has forgotten them.

The core only exposes cumulative counters that reset on every restart, and the
GUI graph is a short window. This watches those counters and keeps one bucket per
local date in a small JSON file.

No dependencies: Python 3.8+ standard library only.

## Why not in the GUI?

This was originally proposed as a Clash Verge Rev feature
([#8175](https://github.com/clash-verge-rev/clash-verge-rev/issues/8175)). It was
declined, because the project has a standing commitment not to collect or analyse
user traffic — the same reason [#7348](https://github.com/clash-verge-rev/clash-verge-rev/issues/7348)
and [#6938](https://github.com/clash-verge-rev/clash-verge-rev/issues/6938) were
closed. A maintainer suggested pulling the numbers from the core's External
Controller with a small script instead. That is what this is: it runs on your own
machine, writes a file you own, and sends nothing anywhere.

## Quick start

Point it at the core's External Controller and let it run:

```bash
python3 mihomo_traffic_logger.py run \
  --controller http://127.0.0.1:9090 \
  --secret your-secret
```

Then review history at any time:

```bash
python3 mihomo_traffic_logger.py report
```

```
DATE             DOWNLOAD       UPLOAD        TOTAL
2026-10-09        4.12 GB     305.71 MB       4.42 GB
2026-10-10        6.85 GB     512.30 MB       7.35 GB
2026-10-11        1.93 GB     128.44 MB       2.06 GB
TOTAL            12.90 GB     946.45 MB      13.83 GB
AVERAGE/DAY       4.30 GB     315.48 MB       4.61 GB
```

`--days N` limits the report to the most recent N days.

## Configuration

Every option has an environment variable, so a service unit can stay short:

| Flag | Environment | Default |
|---|---|---|
| `--controller` | `MIHOMO_CONTROLLER` | `http://127.0.0.1:9090` |
| `--secret` | `MIHOMO_SECRET` | *(empty)* |
| `--data` | `MIHOMO_TRAFFIC_DATA` | platform data directory |
| `--flush-interval` | | `60` seconds between writes |
| `--retention` | | `0` (keep everything) |
| `--verbose` | | print every update |

The default history file is `%APPDATA%\mihomo-traffic-logger\daily.json` on
Windows and `$XDG_DATA_HOME/mihomo-traffic-logger/daily.json` elsewhere.

## Modes

- `run` — connect to `/traffic`, record as traffic happens, reconnect on failure.
  This is the default.
- `sample` — take one reading over HTTP and exit. Use this if you prefer a
  cron job or Task Scheduler entry over a long-running process.
- `report` — print the daily table.

## Enabling the External Controller

**Standalone mihomo** — add to `config.yaml` and restart the core:

```yaml
external-controller: 127.0.0.1:9090
secret: "pick-something"
```

**Clash Verge Rev** — works, but needs one setting turned on first. The app
deliberately keeps its core on an IPC channel (a socket file on Unix, a named pipe
on Windows) rather than a TCP port, and blanks the `external-controller` address
before the core starts. That is why `127.0.0.1:9097` does not answer even though it
appears in the generated config, and why the default is off: an exposed controller
has been a real attack surface for this ecosystem.

To use this tool with the desktop client, open **Settings → Clash Settings →
External**, enable **Enable External Controller**, and set a valid API endpoint and
a strong Core Secret. The core then listens on TCP like a standalone mihomo and the
commands above work unchanged.

Keep the controller bound to loopback. It is unauthenticated apart from the
secret and grants full control of the core, including the ability to change
proxies and rules.

## What it does and does not count

It counts the bytes the core reports through its own counters. That includes
everything the core handles, direct and proxied alike — the core does not
separate them.

Two consequences worth knowing before you rely on the numbers:

- **Records only while it is running.** The core keeps no history, so time when
  this process is stopped cannot be recovered. There is no way to backfill.
- **A delta that spans midnight is credited to the day it was sampled.** With the
  default 60-second flush the error is at most one interval. Use `sample` on a
  frequent schedule if you need tighter boundaries.

## Running it as a service

**systemd** (Linux, alongside a standalone mihomo):

```ini
[Unit]
Description=Mihomo daily traffic logger
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 /opt/mihomo-traffic-logger/mihomo_traffic_logger.py run
Environment=MIHOMO_CONTROLLER=http://127.0.0.1:9090
Environment=MIHOMO_SECRET=your-secret
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

**Task Scheduler** (Windows, one reading per minute):

```powershell
schtasks /create /tn "mihomo-traffic-logger" /sc minute /mo 1 ^
  /tr "pythonw.exe C:\path\to\mihomo_traffic_logger.py sample"
```

`pythonw.exe` avoids a console window. Add `/sc onlogon` with `run` instead if
you would rather keep one long-lived process.

## History file

```json
{
  "version": 1,
  "days": {
    "2026-10-11": { "download": 2073481, "upload": 134682 }
  },
  "last": { "ts": 1791718172, "up": 4398, "down": 46736 }
}
```

Written atomically via a temporary file, so an interrupted write leaves the
previous history intact. `last` holds the previous counter reading; delete it if
you ever want to force a fresh baseline.
