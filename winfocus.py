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

:func:`claim` re-takes the foreground the way a window manager would:
attach to the current foreground thread's input queue, then
``BringWindowToTop`` / ``SetForegroundWindow`` / ``SetFocus`` (detaching
again afterwards), and it restores the widget-level focus Tk had inside the
window (so the focused button or search field keeps the caret). It is called
immediately when the popup is built and retried shortly after the window is
mapped, because the first attempt can still lose the race against the window
actually becoming visible.

Everything here is best-effort and never raises: off Windows only the Tk
calls happen, and a failure just leaves the window exactly as it was.
"""

from __future__ import annotations

import sys
from typing import Optional

# Delays (ms) at which a failed claim is retried. 0 = next event-loop pass
# (the window is mapped by then), 120/350 ms catch the cases where Windows
# only allows the activation a moment later.
CLAIM_RETRY_MS = (0, 120, 350)


def claim(window, retries=CLAIM_RETRY_MS) -> bool:
    """Put *window* in front and give it the keyboard.

    Returns True when the window holds the keyboard after this call. When the
    first attempt does not win, a few retries are scheduled on the Tk event
    loop (each one is a silent no-op once the window is gone) — the foreground
    lock can release a moment after the window is mapped.
    """
    ok = _claim_once(window)
    if not ok:
        for delay in retries:
            try:
                window.after(delay, lambda w=window: _claim_once(w))
            except Exception:
                break
    return ok


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
    """One best-effort focus grab. Never raises."""
    if not _alive(window):
        return False
    already = _tk_focused(window) and _win_foreground(window)
    if already:
        return True

    last = _last_focus(window)

    # --- Tk-level raise (works everywhere, incl. off Windows) -------------
    for call in (
        lambda: window.lift(),
        lambda: window.attributes("-topmost", True),
        lambda: window.focus_force(),
    ):
        try:
            call()
        except Exception:
            pass

    # --- Windows-level foreground ----------------------------------------
    ok = _force_foreground_win(window)

    # Restore the widget-level focus (the Skip button, the search field, ...)
    # so forcing the toplevel never moves the caret to the window frame.
    if last is not None:
        try:
            if last.winfo_exists():
                last.focus_set()
        except Exception:
            pass
    return ok


def _win_foreground(window) -> bool:
    """True when *window* is the Windows foreground window (True off Windows)."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        return int(user32.GetForegroundWindow() or 0) == _hwnd(window)
    except Exception:
        return False


def _hwnd(window) -> int:
    """The Win32 handle of a Tk toplevel (0 when unavailable)."""
    try:
        raw = window.winfo_id()
    except Exception:
        return 0
    if isinstance(raw, int):
        return raw
    try:
        return int(str(raw), 0)   # Tk may hand back a hex/decimal string
    except Exception:
        return 0


def _force_foreground_win(window) -> bool:
    """Steal the foreground for *window* on Windows. True when it now has it.

    The ``AttachThreadInput`` dance is what makes this work from a background
    process: Windows only lets the foreground thread (or a thread attached to
    its input queue) call ``SetForegroundWindow`` successfully. Off Windows,
    or when anything here fails, this is a silent no-op.
    """
    if sys.platform != "win32":
        return True
    hwnd = _hwnd(window)
    if not hwnd:
        return False
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
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, ctypes.c_int, wintypes.UINT]

        foreground = int(user32.GetForegroundWindow() or 0)
        if foreground == hwnd:
            return True
        tid_fg = int(user32.GetWindowThreadProcessId(foreground, None)) if foreground else 0
        tid_me = int(kernel32.GetCurrentThreadId())
        attached = False
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_fg, tid_me, True))
        try:
            user32.ShowWindow(hwnd, 5)          # SW_SHOW (never un-minimises)
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
            user32.SetFocus(hwnd)
            # Keep it above every other topmost window (the popup is topmost;
            # without this another topmost window can stay in front).
            HWND_TOPMOST, SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW = -1, 0x1, 0x2, 0x40
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
        finally:
            if attached:
                user32.AttachThreadInput(tid_fg, tid_me, False)
        return int(user32.GetForegroundWindow() or 0) == hwnd
    except Exception:
        return False
