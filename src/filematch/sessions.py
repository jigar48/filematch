"""In-process session store. Each session owns one temp directory."""

from __future__ import annotations

import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COOKIE_NAME = "filematch_session"


@dataclass
class Session:
    id: str
    directory: Path
    last_seen: float
    files: dict[str, Any] = field(default_factory=dict)
    # Later stages (mapping, compare) keep their state here.
    state: dict[str, Any] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)


class SessionStore:
    def __init__(self, ttl_seconds: int, clock=time.monotonic) -> None:
        self.ttl_seconds = ttl_seconds
        self._clock = clock
        self._sessions: dict[str, Session] = {}
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._sessions)

    def get(self, session_id: str | None) -> Session | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                session.last_seen = self._clock()
            return session

    def create(self) -> Session:
        session_id = secrets.token_urlsafe(32)
        directory = Path(tempfile.mkdtemp(prefix="filematch-"))
        session = Session(id=session_id, directory=directory, last_seen=self._clock())
        with self._lock:
            self._sessions[session_id] = session
        return session

    def delete(self, session_id: str) -> bool:
        with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        _remove_dir(session.directory)
        return True

    def sweep(self) -> int:
        """Delete sessions idle for longer than the TTL. Returns how many were removed."""
        cutoff = self._clock() - self.ttl_seconds
        with self._lock:
            expired = [s for s in self._sessions.values() if s.last_seen < cutoff]
            for session in expired:
                del self._sessions[session.id]
        for session in expired:
            _remove_dir(session.directory)
        return len(expired)

    def clear_all(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            _remove_dir(session.directory)


def _remove_dir(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
