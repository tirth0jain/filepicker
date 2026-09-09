"""Global Alt-key suppression while a FilePicker popup is open (Windows only).

Some third-party programs (e.g. AutoDesk apps) react to Alt+<letter>
combinations system-wide — menu mnemonics, registered hotkeys — so while a
popup is on screen and the user holds Alt to toggle materials, those programs
can fire in the background. A low-level keyboard hook (WH_KEYBOARD_LL)
intercepts every keystroke while a popup exists: Alt-modified keys are
swallowed so NO other program ever sees them, and the same message is
re-posted to the popup's own window so FilePicker behaves exactly as before
(even when the popup is not the focused window).

Config: "block_alt_for_other_apps": true (default) / false. Off Windows this
module is a no-op (install() returns False).
"""

from __future__ import annotations

import sys

# HWNDs of every FilePicker window currently open that asked for Alt
# blocking (popup + its dialogs). The hook stays installed while this list
# is non-empty — closing ONE popup (e.g. the "file already exists" dialog
# shown after a popup released itself) must never disable blocking while
# another FilePicker window is still on screen, or third-party programs
# would react to Alt chords again. The NEWEST window is the re-post target.
_HWNDS: list = []
_POPUP_HWND = 0     # HWND of the topmost open FilePicker window (0 = none)
_HOOK = None        # HHOOK while installed
_CALLBACK = None    # keeps the ctypes callback object alive

# Virtual-key codes.
_VK_SHIFT = 0x10
_VK_CONTROL = 0x11
_VK_MENU = 0x12
_VK_LWIN = 0x5B
_VK_RWIN = 0x5C
_VK_TAB = 0x09
_VK_ESCAPE = 0x1B
_VK_SNAPSHOT = 0x2C
_VK_APPS = 0x5D
_VK_PROCESSKEY = 0xE5
_VK_PACKET = 0xE7

_WM_KEYDOWN = 0x0100
_WM_KEYUP = 0x0101
_WM_SYSKEYDOWN = 0x0104
_WM_SYSKEYUP = 0x0105

# System combos that must always reach Windows untouched (Alt+Tab = task
# switch, Alt+F4 = close window, Alt+Esc, Alt+PrintScreen = screenshot...),
# plus IME/input-method pseudo-keys (VK_PROCESSKEY, VK_PACKET).
_ALWAYS_PASS = {
    _VK_TAB, _VK_ESCAPE, _VK_SNAPSHOT, _VK_APPS,
    _VK_PROCESSKEY, _VK_PACKET,
    *range(0x70, 0x80),  # F1..F12
}

# Other modifier keys are never swallowed themselves (their bare presses are
# harmless); only the keys pressed *while Alt is held* are withheld, and the
# Alt key itself.
_MODIFIERS = {_VK_MENU, _VK_SHIFT, _VK_CONTROL, _VK_LWIN, _VK_RWIN}


def should_swallow(vk: int, alt_down: bool, win_down: bool) -> bool:
    """Decision rule: whether a virtual key must be withheld from other apps.

    Pure logic, unit-testable. While a popup is open (implied by the caller)
    the Alt key itself is always swallowed — no other program ever arms its
    menu bar — and every other key is swallowed only while Alt is physically
    held and Windows is NOT held (Win+... combos stay system-level). System
    combos (F-keys, Tab, Esc, PrintScreen, IME keys) always pass.
    """
    if vk == _VK_MENU:
        return True
    if vk in _MODIFIERS:
        return False
    if not alt_down or win_down:
        return False
    return vk not in _ALWAYS_PASS


def install(popup_hwnd) -> bool:
    """Start intercepting Alt combos while a FilePicker window is open.

    Ref-counted: each open window (popup, duplicate dialog, ...) installs
    itself with its own hwnd, and :func:`remove` only unhooks once every
    window has closed. Installing again with a new hwnd while the hook is
    already active just counts the window and retargets the re-post to the
    newest one. Returns True when the hook is active, False off Windows or
    when installation failed.
    """
    global _POPUP_HWND, _HOOK, _CALLBACK
    if sys.platform != "win32":
        return False
    # winfo_id() may come back as an int or a hex/decimal string.
    try:
        hwnd = int(str(popup_hwnd), 0) if popup_hwnd else 0
    except (TypeError, ValueError):
        hwnd = 0
    if not hwnd:
        return False
    if hwnd in _HWNDS:
        # Same window installing twice: just retarget — no double count.
        _POPUP_HWND = hwnd
        return _HOOK is not None
    if _HOOK is not None:
        # Hook already active for another window: count + retarget.
        _HWNDS.append(hwnd)
        _POPUP_HWND = hwnd
        return True
    if not _setup_hook():
        return False
    _HWNDS.append(hwnd)
    _POPUP_HWND = hwnd
    return True


def _setup_hook() -> bool:
    """Install the WH_KEYBOARD_LL hook (Windows only, called with no hook
    currently active). Returns True when the hook is up."""
    global _HOOK, _CALLBACK
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:
        return False

    class KBDLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("vkCode", wintypes.DWORD),
            ("scanCode", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    HOOKPROC = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
    )

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    # Correct 64-bit signatures (default ctypes would truncate handles).
    user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
    user32.GetAsyncKeyState.restype = wintypes.SHORT
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, HOOKPROC, ctypes.c_void_p, wintypes.DWORD,
    ]
    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.UnhookWindowsHookEx.restype = wintypes.BOOL
    user32.CallNextHookEx.argtypes = [
        wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.CallNextHookEx.restype = ctypes.c_long

    def _hook_proc(nCode, wParam, lParam):
        if nCode < 0 or not _POPUP_HWND:
            return user32.CallNextHookEx(_HOOK, nCode, wParam, lParam)
        if wParam not in (_WM_KEYDOWN, _WM_SYSKEYDOWN, _WM_KEYUP, _WM_SYSKEYUP):
            return user32.CallNextHookEx(_HOOK, nCode, wParam, lParam)
        kbd = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
        vk = int(kbd.vkCode)
        if wParam in (_WM_KEYUP, _WM_SYSKEYUP) and vk != _VK_MENU:
            # Bare key-ups trigger nothing anywhere; only the Alt release
            # matters (so the popup's chord engine sees it).
            return user32.CallNextHookEx(_HOOK, nCode, wParam, lParam)
        alt_down = bool(user32.GetAsyncKeyState(_VK_MENU) & 0x8000)
        win_down = bool(
            user32.GetAsyncKeyState(_VK_LWIN) & 0x8000
            or user32.GetAsyncKeyState(_VK_RWIN) & 0x8000
        )
        if should_swallow(vk, alt_down, win_down):
            # Swallow for every other program; forward only to our window so
            # FilePicker's own Alt chords keep working (even unfocused).
            user32.PostMessageW(_POPUP_HWND, wParam, vk, lParam)
            return 1  # handled — no other app ever sees this key
        return user32.CallNextHookEx(_HOOK, nCode, wParam, lParam)

    _CALLBACK = HOOKPROC(_hook_proc)  # keep the callback alive
    _HOOK = user32.SetWindowsHookExW(
        13,  # WH_KEYBOARD_LL
        _CALLBACK,
        kernel32.GetModuleHandleW(None),
        0,   # hook is global (low-level hooks can't be thread-specific)
    )
    if not _HOOK:
        _CALLBACK = None
        return False
    return True


def remove(hwnd=None) -> None:
    """Stop intercepting Alt combos for *hwnd* (or the newest window).

    Ref-counted: the hook stays active while ANY FilePicker window is still
    open, retargeted to the newest remaining one; it is unhooked only when
    the last window closes. ``hwnd`` may be omitted for backward
    compatibility (removes the newest window).
    """
    global _POPUP_HWND, _HOOK, _CALLBACK
    if hwnd is not None:
        try:
            hwnd = int(str(hwnd), 0) if hwnd else 0
        except (TypeError, ValueError):
            hwnd = 0
        if hwnd in _HWNDS:
            _HWNDS.remove(hwnd)
    elif _HWNDS:
        _HWNDS.pop()
    if _HWNDS:
        _POPUP_HWND = _HWNDS[-1]
        return
    _POPUP_HWND = 0
    if _HOOK is not None and sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.user32.UnhookWindowsHookEx(_HOOK)
        except Exception:
            pass
    _HOOK = None
    _CALLBACK = None