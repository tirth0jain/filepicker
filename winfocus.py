"""Bring a FilePicker window to the front AND give it the keyboard.

Why this exists
---------------
The popup is created by a tray application that is normally NOT the
foreground process: a download finishes while the user is in the browser or
AutoCAD, and Windows' foreground lock then refuses the activation. Tk's
``focus_force()`` is a plain ``SetFocus`` on the window, which Windows
silently ignores for a process that is not already in the foreground — the
popup appeared on top (it is topmost) but every keystroke still went to the
other program: Ctrl+S did nothing and the Alt material chords only worked
because the global hook re-posts them to our window.

:func:`claim` fixes that in two clearly separated halves:

* on the Tk/main thread — ``lift``, ``-topmost`` and ``focus_force`` (cheap,
  safe, works everywhere), plus the widget-level focus restore;
* off the main thread — the Win32 foreground steal (attach to the current
  foreground thread's input queue, then ``BringWindowToTop`` /
  ``SetForegroundWindow`` / ``SetFocus``).

The Win32 half **never runs on the UI thread**: ``AttachThreadInput`` +
``SetForegroundWindow`` activate another process' window, and if that
process is busy (AutoCAD rendering, a browser modal, ...) the call can block
until it pumps messages. A popup that hangs the main thread would freeze the
whole app *and* leave the popup invisible, so that work happens in a daemon
thread that the UI never waits for.

Nothing here ever changes a window's visibility: Tk (and CustomTkinter's own
titlebar handling) owns that. :func:`visible` only *reports* whether the
window is really on screen — the popup uses it as a watchdog.

Everything is best-effort and never raises: off Windows only the Tk calls
happen, and a failure just leaves the window exactly as it was.
"""

from __future__ import annotations

import sys
import threading
from typing import Optional

# Delays (ms) at which a failed claim is retried. 0 = next event-loop pass
# (the window is mapped by then), 120/350 ms catch the cases where Windows
# only allows the activation a moment later.
CLAIM_RETRY_MS = (0, 120, 350)

# Upper bound on foreground-steal threads that may be alive at once. Each one
# is a daemon that normally finishes in microseconds; the cap only exists so
# that a permanently hung foreground application (where the steal can never
# return) cannot spawn an unbounded number of threads.
_MAX_STEAL_THREADS = 4

_steal_lock = threading.Lock()
_steal_threads: list = []


def claim(window, retries=CLAIM_RETRY_MS) -> bool:
    """Put *window* in front and give it the keyboard.

    Returns True when the window already holds the keyboard (or the Tk half
    succeeded); the Win32 half is asynchronous and never blocks the caller.
    When the window does not have the keyboard yet, a few retries are
    scheduled on the Tk event loop (each a silent no-op once the window is
    gone) — the foreground lock can release a moment after the window is
    mapped.
    """
    ok = _claim_once(window)
    if not ok:
        for delay in retries:
            try:
                window.after(delay, lambda w=window: _claim_once(w))
            except Exception:
                break
    return ok


def visible(window) -> bool:
    """True when *window* is really on screen.

    Tk's ``winfo_viewable`` is not enough on Windows: a window can be mapped
    as far as Tk knows while Win32 keeps it hidden (CustomTkinter briefly
    withdraws every new window to colour its title bar, and a mis-ordered
    revert can leave it that way). On Windows the answer therefore comes from
    ``IsWindowVisible`` on the real top-level handle.
    """
    if sys.platform == "win32":
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            user32.IsWindowVisible.argtypes = [wintypes.HWND]
            user32.IsWindowVisible.restype = wintypes.BOOL
            hwnd = _hwnd(window)
            if hwnd:
                return bool(user32.IsWindowVisible(hwnd))
        except Exception:
            pass  # fall through to the Tk answer
    try:
        return bool(window.winfo_viewable())
    except Exception:
        return False


def _alive(window) -> bool:
    """True when *window* still exists (destroyed windows are never touched)."""
    try:
        return bool(window.winfo_exists())
    except Exception:
        return False


def _tk_focused(window) -> bool:
    """True when Tk already gives the keyboard to a widget of *window*."""
    try:
        focused = window.focus_displayof()
    except Exception:
        return False
    if focused is None:
        return False
    try:
        return focused.winfo_toplevel() == window
    except Exception:
        return False


def _last_focus(window):
    """The widget inside *window* that last had the keyboard (or None)."""
    try:
        last = window.focus_lastfor()
    except Exception:
        return None
    try:
        if last is not None and last.winfo_exists():
            return last
    except Exception:
        pass
    return None


def _claim_once(window) -> bool:
    """One best-effort focus grab. Never raises, never blocks."""
    if not _alive(window):
        return False
    already = _tk_focused(window)
    if already and (sys.platform != "win32" or _win_foreground(window)):
        return True

    last = _last_focus(window)

    # --- Tk-level raise (main thread; works everywhere, incl. off Windows) --
    for call in (
        lambda: window.lift(),
        lambda: window.attributes("-topmost", True),
        lambda: window.focus_force(),
    ):
        try:
            call()
        except Exception:
            pass

    # --- Windows-level foreground, OFF the UI thread -----------------------
    # Only for a window that is actually on screen: poking a withdrawn window
    # (CustomTkinter withdraws every new window for ~5ms to colour its title
    # bar) is what can confuse its re-show bookkeeping.
    if sys.platform == "win32" and _alive(window):
        try:
            if window.winfo_viewable():
                _steal_foreground_async(window)
        except Exception:
            pass

    # Restore the widget-level focus (the Skip button, the search field, ...)
    # so forcing the toplevel never moves the caret to the window frame.
    if last is not None:
        try:
            if last.winfo_exists():
                last.focus_set()
        except Exception:
            pass
    return already


def _win_foreground(window) -> bool:
    """True when *window* is the Windows foreground window (True off Windows)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        hwnd = _hwnd(window)
        return bool(hwnd) and int(user32.GetForegroundWindow() or 0) == hwnd
    except Exception:
        return False


def _hwnd(window) -> int:
    """The real Win32 top-level handle of a Tk window (0 when unavailable).

    ``winfo_id()`` returns Tk's own window, which on Windows is a CHILD of the
    window-manager frame — and ``SetForegroundWindow``/``IsWindowVisible``
    need the frame. ``wm_frame()`` returns exactly that (as a hex string).
    """
    try:
        frame = window.wm_frame()
    except Exception:
        frame = None
    if frame:
        try:
            return int(str(frame), 16)
        except Exception:
            pass
    try:
        raw = window.winfo_id()
    except Exception:
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 0)
    except Exception:
        return 0


def _steal_foreground_async(window) -> None:
    """Run the Win32 foreground steal in a daemon thread (UI never waits)."""
    hwnd = _hwnd(window)
    if not hwnd:
        return
    with _steal_lock:
        _steal_threads[:] = [t for t in _steal_threads if t.is_alive()]
        if len(_steal_threads) >= _MAX_STEAL_THREADS:
            return
        worker = threading.Thread(
            target=_force_foreground_win, args=(hwnd,), daemon=True,
            name="filepicker-focus")
        _steal_threads.append(worker)
    try:
        worker.start()
    except Exception:
        pass


def _force_foreground_win(hwnd: int) -> bool:
    """Steal the foreground for *hwnd* on Windows. True when it now has it.

    The ``AttachThreadInput`` dance is what makes this work from a background
    process: Windows only lets the foreground thread (or a thread attached to
    its input queue) call ``SetForegroundWindow`` successfully. Off Windows,
    or when anything here fails, this is a silent no-op. Called from a worker
    thread — it may block while the other application is busy, which is
    exactly why it must not run on the UI thread.
    """
    if sys.platform != "win32" or not hwnd:
        return True
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False
    try:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # Correct 64-bit signatures (default ctypes truncates handles).
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.AttachThreadInput.argtypes = [
            wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        user32.AttachThreadInput.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetFocus.argtypes = [wintypes.HWND]

        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground == hwnd:
            return True
        tid_fg = int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        tid_me = int(kernel32.GetCurrentThreadId())
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_fg, tid_me, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(tid_fg, tid_me, False)
        return int(user32.GetForegroundWindow() or 0) == hwnd
    except Exception:
        return False
