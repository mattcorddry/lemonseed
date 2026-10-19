"""Minimal journald client for the standard logging framework.

systemd-python's ``JournalHandler``/``journal.send`` does this too, but it is a
C extension to package for the appliance; the wire protocol it speaks is a
datagram of newline-separated ``KEY=VALUE`` fields to journald's socket. That
is all this module does, so the control plane's logs can ride the standard
``logging`` framework and land in journald under their own identifier — with
journald's rotation, retention and filtering — without a new dependency.
"""

from __future__ import annotations

import logging
import socket
from pathlib import Path

JOURNAL_SOCKET = Path("/run/systemd/journal/socket")

# LogRecord attributes that are not journald fields; everything else an access
# line passes via ``extra={...}`` is forwarded as an uppercase journald field.
_STD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "module", "msecs", "message",
    "msg", "name", "pathname", "process", "processName", "relativeCreated",
    "stack_info", "taskName", "thread", "threadName",
})


class JournalHandler(logging.Handler):
    """Send every record to journald under one identifier.

    MESSAGE carries the formatted line; extras arrive as uppercase journald
    fields (``CLIENT=…``, ``USER=…``), which is what makes the trail
    ``journalctl -t <identifier> USER=matt``-filterable. Sending is
    best-effort: no journald socket (a dev box, a unit test) means the line is
    dropped — a log line must never break a request.
    """

    def __init__(self, identifier: str, socket_path: Path = JOURNAL_SOCKET) -> None:
        super().__init__()
        self.identifier = identifier
        self.socket_path = socket_path

    def emit(self, record: logging.LogRecord) -> None:
        try:
            fields = ["MESSAGE=" + self.format(record),
                      "SYSLOG_IDENTIFIER=" + self.identifier]
            for key, value in record.__dict__.items():
                if key not in _STD_ATTRS:
                    fields.append(f"{key.upper()}={value}")
            payload = ("\n".join(fields)).encode("utf-8", "replace")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                sock.sendto(payload, str(self.socket_path))
        except (OSError, ValueError):
            pass
