"""Folder watcher for FilePicker.

Uses ``watchdog`` to monitor the configured download folder. Temporary files
(``.crdownload``, ``.part``, ``.tmp``) and hidden files are ignored. When a
file appears or changes we wait for it to stabilise: the size stops growing and
the file is no longer locked by another process (the browser has finished
writing). Only then is the file handed off to the callback.

Completed files are queued so several simultaneous downloads are handled one at
a time.
"""

from __future__ import annotations

import queue
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# Extensions that browsers/other tools use while still writing a file.
_TEMP_SUFFIXES = (".crdownload", ".part", ".tmp", ".download", ".opdownload", ".partial")
# The default size to assume a file has when only metadata is available.
_DEFAULT_SIZE = 0
# Seconds to require a stable size before declaring a file "done".
_STABLE_WINDOW = 0.4
# Retry back-off for files that are still locked.
_LOCK_RETRY_DELAY = 0.1


def is_temp_name(name: str) -> bool:
    """Return True if ``name`` looks like an in-progress or hidden file."""
    lowered = name.lower()
    if lowered.startswith("."):          # hidden files (macOS zip artefacts, etc.)
        return True
    return lowered.endswith(_TEMP_SUFFIXES)


def _is_locked(path: Path) -> bool:
    """Try to open the file for exclusive write access to test the lock.

    On Windows, opening with ``a+b`` raises PermissionError while another
    process still holds a write lock on the file.
    """
    try:
        with open(path, "a+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        # Missing mid-check or some other OS error -> treat as not ready.
        return True


class _StableTracker:
    """Tracks size/size-time per path so we only fire once a file settles."""

    def __init__(self, stable_window: float = _STABLE_WINDOW) -> None:
        self._stable_window = stable_window
        self._sizes: dict = {}      # path -> (size, last_change_time)
        self._lock = threading.Lock()

    def is_stable(self, path: Path, size: int) -> bool:
        """Return True when ``size`` has been unchanged for the stable window."""
        key = str(path)
        now = time.monotonic()
        with self._lock:
            prev = self._sizes.get(key)
            if prev is None or prev[0] != size:
                self._sizes[key] = (size, now)
                return False
            return (now - prev[1]) >= self._stable_window


class WatcherHandler(FileSystemEventHandler):
    """watchdog handler that debounces completion of downloaded files."""

    def __init__(
        self,
        on_completed: Callable[[Path], None],
        handle_dirs: bool = False,
    ) -> None:
        super().__init__()
        self.on_completed = on_completed
        self.handle_dirs = handle_dirs
        self._tracker = _StableTracker()
        self._recently_handled: Dict[str, float] = {}
        self._handled_lock = threading.Lock()
        self._handled_ttl = 60.0

    # -- watchdog overrides --------------------------------------------
    def on_created(self, event) -> None:
        self._maybe_queue(event)

    def on_modified(self, event) -> None:
        self._maybe_queue(event)

    def on_moved(self, event) -> None:
        # A file finished downloading often lands first as a .crdownload then
        # gets renamed to its final name. Handle the destination.
        self._maybe_queue_path(Path(event.dest_path))

    def on_deleted(self, event) -> None:
        # A file was removed (e.g. organised away). Forget it so a later
        # download with the same name is detected again.
        key = str(Path(event.src_path))
        with self._handled_lock:
            self._recently_handled.pop(key, None)

    def _maybe_queue(self, event) -> None:
        if event.is_directory and not self.handle_dirs:
            return
        self._maybe_queue_path(Path(event.src_path))

    def _maybe_queue_path(self, path: Path) -> None:
        name = path.name
        if not name or is_temp_name(name):
            return

        # Only hand a file off once; remember it so modified events don't
        # re-trigger after it's moved out of the watch folder. Prune stale
        # entries so the map never grows unbounded.
        key = str(path)
        now = time.monotonic()
        with self._handled_lock:
            if len(self._recently_handled) > 500:
                stale = [k for k, t in self._recently_handled.items()
                         if now - t > self._handled_ttl]
                for k in stale:
                    del self._recently_handled[k]
            if key in self._recently_handled:
                return
            self._recently_handled[key] = now

        self.on_completed(path)


def wait_until_stable(
    path: Path,
    on_completed: Callable[[Path], None],
    stable_window: float = _STABLE_WINDOW,
    max_attempts: int = 120,
) -> None:
    """Block until ``path`` is no longer growing and is not locked.

    Runs in its own worker thread. Calls ``on_completed`` once the file is
    ready, or logs a warning if it never settles in time.
    """
    last_size = _DEFAULT_SIZE
    stable_since: Optional[float] = None
    attempts = 0

    while attempts < max_attempts:
        try:
            size = path.stat().st_size if path.exists() else _DEFAULT_SIZE
        except OSError:
            size = _DEFAULT_SIZE

        locked = _is_locked(path) if path.exists() else True

        if not locked and size == last_size:
            if stable_since is None:
                stable_since = time.monotonic()
            elif time.monotonic() - stable_since >= stable_window:
                on_completed(path)
                return
        else:
            stable_since = None
            last_size = size

        attempts += 1
        time.sleep(_LOCK_RETRY_DELAY)

    print(f"[watcher] {path.name} never stabilised after {max_attempts} checks; "
          f"handing off anyway.")
    on_completed(path)


class DownloadWatcher:
    """High-level watcher that debounces completions and feeds a callback."""

    def __init__(
        self,
        watch_directory: str,
        on_completed: Callable[[Path], None],
        stable_window: float = _STABLE_WINDOW,
    ) -> None:
        self.watch_directory = str(watch_directory)
        self.on_completed = on_completed
        self.stable_window = stable_window
        self._queue: "queue.Queue[Path]" = queue.Queue()
        self._observer: Optional[Observer] = None
        self._worker: Optional[threading.Thread] = None
        self._running = False

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Begin watching and process completions in a background thread."""
        target = Path(self.watch_directory)
        target.mkdir(parents=True, exist_ok=True)

        def handler_callback(path: Path) -> None:
            # Debounce in a dedicated worker thread. This MUST NOT run on the
            # watchdog dispatch thread: wait_until_stable sleeps, and blocking
            # that thread would stall detection of every subsequent file.
            threading.Thread(
                target=wait_until_stable,
                args=(path, self._queue.put, self.stable_window),
                name="filepicker-debounce",
                daemon=True,
            ).start()

        event_handler = WatcherHandler(handler_callback)
        self._observer = Observer()
        self._observer.schedule(event_handler, str(target), recursive=False)
        self._observer.daemon = True
        self._observer.start()

        self._worker = threading.Thread(
            target=self._consume, name="filepicker-consumer", daemon=True
        )
        self._running = True
        self._worker.start()

    def _consume(self) -> None:
        while self._running:
            try:
                path = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                self.on_completed(path)
            except Exception as exc:  # noqa: BLE001 - keep watcher alive
                print(f"[watcher] consumer error for {path}: {exc}")

    def stop(self) -> None:
        self._running = False
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=2)
