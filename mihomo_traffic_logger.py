#!/usr/bin/env python3
"""Record per-day traffic totals from a running mihomo core.

The core exposes cumulative up/down byte counters on its /traffic websocket.
This tool watches those counters and accumulates the deltas into one bucket per
local date, so past days can be reviewed after the fact.

The core resets its counters when it restarts, so a sample whose totals moved
backwards is treated as a new baseline rather than a negative delta.

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import ssl
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

DEFAULT_CONTROLLER = "http://127.0.0.1:9090"
DATA_VERSION = 1
RECONNECT_DELAY = 3.0
MAX_RECONNECT_DELAY = 30.0
HEADER_TIMEOUT = 10.0


def default_data_path() -> str:
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "mihomo-traffic-logger", "daily.json")
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "mihomo-traffic-logger", "daily.json")


def format_bytes(value: float) -> str:
    units = ["B", "KB", "MB", "GB", "TB", "PB"]
    index = 0
    value = float(value)
    while value >= 1024 and index < len(units) - 1:
        value /= 1024.0
        index += 1
    return "%.2f %s" % (value, units[index])


class Store:
    """Daily totals plus the last observed counters, kept in one JSON file."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.days: dict[str, dict[str, int]] = {}
        self.last: dict[str, object] | None = None
        self._loaded = False

    def load(self) -> None:
        self._loaded = True
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            print("warning: cannot read %s (%s); starting fresh" % (self.path, exc),
                  file=sys.stderr)
            return
        self.days = raw.get("days") or {}
        self.last = raw.get("last")

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        payload = {"version": DATA_VERSION, "days": self.days, "last": self.last}
        tmp = self.path + ".tmp"
        # Replace rather than truncate so a crash mid-write cannot lose the history.
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, self.path)

    def record(self, day: str, up_delta: int, down_delta: int) -> None:
        bucket = self.days.setdefault(day, {"upload": 0, "download": 0})
        bucket["upload"] += up_delta
        bucket["download"] += down_delta

    def prune(self, retention_days: int) -> None:
        if retention_days <= 0:
            return
        cutoff = date.today() - timedelta(days=retention_days)
        for key in list(self.days):
            try:
                day = datetime.strptime(key, "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                del self.days[key]


class TrafficClient:
    """Reads the core's /traffic websocket, reconnecting as needed."""

    def __init__(self, controller: str, secret: str) -> None:
        parsed = urllib.parse.urlparse(controller)
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            raise ValueError("controller must be an http(s):// URL, got %r" % controller)
        if not parsed.hostname:
            raise ValueError("controller URL has no host: %r" % controller)
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme in ("https", "wss") else 80)
        self.tls = parsed.scheme in ("https", "wss")
        self.secret = secret
        self.sock: socket.socket | None = None
        self.buffer = b""

    def connect(self) -> None:
        raw = socket.create_connection((self.host, self.port), timeout=HEADER_TIMEOUT)
        if self.tls:
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=self.host)
        key = base64.b64encode(os.urandom(16)).decode()
        request = [
            "GET /traffic HTTP/1.1",
            "Host: %s:%d" % (self.host, self.port),
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Key: %s" % key,
            "Sec-WebSocket-Version: 13",
        ]
        if self.secret:
            request.append("Authorization: Bearer %s" % self.secret)
        raw.sendall(("\r\n".join(request) + "\r\n\r\n").encode())

        buffer = b""
        while b"\r\n\r\n" not in buffer:
            chunk = raw.recv(4096)
            if not chunk:
                raw.close()
                raise ConnectionError("core closed the connection during handshake")
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
        if " 101" not in status_line:
            raw.close()
            raise ConnectionError("core refused the websocket upgrade: %s" % status_line)

        self.sock = raw
        self.buffer = rest

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None
                self.buffer = b""

    def _fill(self, needed: int) -> None:
        while len(self.buffer) < needed:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("core closed the traffic stream")
            self.buffer += chunk

    def next_totals(self) -> tuple[int, int] | None:
        """Block for the next counter sample. Returns None for non-data frames."""
        while True:
            self._fill(2)
            first, second = self.buffer[0], self.buffer[1]
            opcode = first & 0x0F
            length = second & 0x7F
            offset = 2
            if length == 126:
                self._fill(4)
                length = int.from_bytes(self.buffer[2:4], "big")
                offset = 4
            elif length == 127:
                self._fill(10)
                length = int.from_bytes(self.buffer[2:10], "big")
                offset = 10
            if second & 0x80:
                self._fill(offset + 4)
                offset += 4
            self._fill(offset + length)
            payload = self.buffer[offset:offset + length]
            self.buffer = self.buffer[offset + length:]

            if opcode == 0x8:
                raise ConnectionError("core sent a websocket close frame")
            if opcode != 0x1:
                continue
            try:
                message = json.loads(payload.decode("utf-8", "replace"))
            except ValueError:
                continue
            up_total = message.get("upTotal")
            down_total = message.get("downTotal")
            if up_total is None or down_total is None:
                continue
            return int(up_total), int(down_total)


def accumulate(store: Store, up_total: int, down_total: int) -> tuple[int, int]:
    """Fold one sample into today's bucket. Returns the applied delta."""
    now = int(time.time())
    previous = store.last
    store.last = {"ts": now, "up": up_total, "down": down_total}

    if not isinstance(previous, dict):
        return 0, 0
    try:
        last_up = int(previous["up"])
        last_down = int(previous["down"])
    except (KeyError, TypeError, ValueError):
        return 0, 0

    up_delta = up_total - last_up
    down_delta = down_total - last_down
    if up_delta < 0 or down_delta < 0:
        # The core restarted and its counters began again from zero.
        return 0, 0

    day = date.today().isoformat()
    store.record(day, up_delta, down_delta)
    return up_delta, down_delta


def command_run(args: argparse.Namespace, store: Store) -> int:
    store.load()
    store.prune(args.retention)
    client = TrafficClient(args.controller, args.secret)
    delay = RECONNECT_DELAY
    pending = False
    last_write = time.time()

    print("watching %s -> %s" % (args.controller, store.path))
    try:
        while True:
            try:
                if client.sock is None:
                    client.connect()
                    delay = RECONNECT_DELAY
                    print("connected to the core")
                totals = client.next_totals()
                if totals is None:
                    continue
                up_delta, down_delta = accumulate(store, totals[0], totals[1])
                if up_delta or down_delta:
                    pending = True
                    if args.verbose:
                        bucket = store.days[date.today().isoformat()]
                        print("%s  +%s down  +%s up  (today %s / %s)"
                              % (datetime.now().strftime("%H:%M:%S"),
                                 format_bytes(down_delta), format_bytes(up_delta),
                                 format_bytes(bucket["download"]),
                                 format_bytes(bucket["upload"])))
                # Flush on a timer so a busy link does not rewrite the file constantly,
                # and a quiet one still lands on disk promptly.
                if pending and (time.time() - last_write) >= args.flush_interval:
                    store.save()
                    pending = False
                    last_write = time.time()
            except KeyboardInterrupt:
                raise
            except (OSError, ConnectionError, ValueError) as exc:
                if pending:
                    store.save()
                    pending = False
                client.close()
                print("core unavailable (%s); retrying in %.0fs" % (exc, delay), file=sys.stderr)
                time.sleep(delay)
                delay = min(delay * 2, MAX_RECONNECT_DELAY)
    except KeyboardInterrupt:
        print()
    finally:
        client.close()
        store.save()
        print("saved %s" % store.path)
    return 0


def command_report(args: argparse.Namespace, store: Store) -> int:
    store.load()
    if not store.days:
        print("no traffic recorded yet (%s)" % store.path)
        return 0

    rows = sorted(store.days.items())
    if args.days:
        rows = rows[-args.days:]

    print("%-12s %12s %12s %12s" % ("DATE", "DOWNLOAD", "UPLOAD", "TOTAL"))
    total_down = total_up = 0
    for day, bucket in rows:
        down = int(bucket.get("download", 0))
        up = int(bucket.get("upload", 0))
        total_down += down
        total_up += up
        print("%-12s %12s %12s %12s"
              % (day, format_bytes(down), format_bytes(up), format_bytes(down + up)))

    if len(rows) > 1:
        print("%-12s %12s %12s %12s"
              % ("TOTAL", format_bytes(total_down), format_bytes(total_up),
                 format_bytes(total_down + total_up)))
        print("%-12s %12s" % ("AVERAGE/DAY", format_bytes(total_down / len(rows)),
                              format_bytes(total_up / len(rows))))
    return 0


def command_sample(args: argparse.Namespace, store: Store) -> int:
    """Take a single reading over HTTP. Useful for cron-style scheduling."""
    store.load()
    store.prune(args.retention)
    url = args.controller.rstrip("/") + "/connections"
    request = urllib.request.Request(url, headers={"User-Agent": "mihomo-traffic-logger"})
    if args.secret:
        request.add_header("Authorization", "Bearer %s" % args.secret)
    with urllib.request.urlopen(request, timeout=HEADER_TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8", "replace"))

    up_delta, down_delta = accumulate(store, int(payload.get("uploadTotal") or 0),
                                      int(payload.get("downloadTotal") or 0))
    store.save()
    if args.verbose:
        print("+%s down  +%s up" % (format_bytes(down_delta), format_bytes(up_delta)))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Record per-day traffic totals from a running mihomo core.")
    parser.add_argument("command", nargs="?", default="run",
                        choices=("run", "report", "sample"),
                        help="run: watch and record (default); report: print history; "
                             "sample: take one reading and exit")
    parser.add_argument("--controller",
                        default=os.environ.get("MIHOMO_CONTROLLER", DEFAULT_CONTROLLER),
                        help="core external controller URL (default: %(default)s)")
    parser.add_argument("--secret",
                        default=os.environ.get("MIHOMO_SECRET", ""),
                        help="controller secret, if one is configured")
    parser.add_argument("--data",
                        default=os.environ.get("MIHOMO_TRAFFIC_DATA") or default_data_path(),
                        help="history file (default: %(default)s)")
    parser.add_argument("--days", type=int, default=0,
                        help="report: only show the most recent N days")
    parser.add_argument("--retention", type=int, default=0,
                        help="drop records older than N days (default: keep everything)")
    parser.add_argument("--flush-interval", type=float, default=60.0,
                        help="seconds between history writes (default: %(default)s)")
    parser.add_argument("--verbose", action="store_true", help="print each update")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Under a service manager or a redirected log file stdout is block-buffered,
    # which hides progress until the buffer fills. Line buffering keeps it visible.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
    store = Store(args.data)
    try:
        if args.command == "report":
            return command_report(args, store)
        if args.command == "sample":
            return command_sample(args, store)
        return command_run(args, store)
    except (OSError, ConnectionError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
