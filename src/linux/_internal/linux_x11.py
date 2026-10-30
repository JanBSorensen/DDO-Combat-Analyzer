from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

_CACHE = {"xid": None, "checked": 0.0}
_TLS = threading.local()
_DDO_PATTERNS = (
    "dungeons & dragons online",
    "dungeons and dragons online",
    "dndclient",
    "dndclient64",
    "turbine device class",
)


def _require_x11():
    session = os.environ.get("XDG_SESSION_TYPE", "").strip().lower()
    if session and session != "x11":
        raise RuntimeError(
            "This Linux build currently targets X11. Log out and choose a Cinnamon (X11) session."
        )
    if not os.environ.get("DISPLAY"):
        raise RuntimeError("No X11 DISPLAY is available.")


def _display():
    _require_x11()
    d = getattr(_TLS, "display", None)
    if d is None:
        try:
            from Xlib import display
        except Exception as exc:
            raise RuntimeError("python-xlib is not installed. Run SETUP DDO Combat Analyzer.sh.") from exc
        d = display.Display()
        _TLS.display = d
    return d


def _window_text(win):
    title = ""
    wm_class = ()
    try:
        atom_name = _display().intern_atom("_NET_WM_NAME", only_if_exists=True)
        atom_utf8 = _display().intern_atom("UTF8_STRING", only_if_exists=True)
        if atom_name and atom_utf8:
            prop = win.get_full_property(atom_name, atom_utf8)
            if prop and prop.value:
                raw = prop.value
                if isinstance(raw, bytes):
                    title = raw.decode("utf-8", "replace")
                else:
                    title = str(raw)
    except Exception:
        pass
    if not title:
        try:
            title = win.get_wm_name() or ""
        except Exception:
            title = ""
    try:
        wm_class = win.get_wm_class() or ()
    except Exception:
        wm_class = ()
    return str(title), tuple(str(x) for x in wm_class)


def _client_window_ids():
    d = _display()
    root = d.screen().root
    ids = []
    for prop_name in ("_NET_CLIENT_LIST_STACKING", "_NET_CLIENT_LIST"):
        try:
            atom = d.intern_atom(prop_name, only_if_exists=True)
            if not atom:
                continue
            from Xlib import X
            prop = root.get_full_property(atom, X.AnyPropertyType)
            if prop is not None and getattr(prop, "value", None) is not None:
                ids = [int(x) for x in prop.value]
                if ids:
                    return ids
        except Exception:
            pass
    try:
        return [int(w.id) for w in root.query_tree().children]
    except Exception:
        return []


def _score_window(title: str, wm_class: tuple[str, ...]) -> int:
    text = " ".join((title,) + wm_class).lower()
    if any(bad in text for bad in ("launcher", "patchclient", "akamai")):
        return -100
    score = 0
    if "dungeons & dragons online" in text or "dungeons and dragons online" in text:
        score += 20
    if "dndclient64" in text or "dndclient" in text:
        score += 16
    if "turbine device class" in text:
        score += 12
    if "ddo" in text:
        score += 2
    return score


def find_ddo_window(force: bool = False):
    """Return the X11 client-window id for the running DDO game, or None."""
    now = time.monotonic()
    if not force and _CACHE["xid"] and now - _CACHE["checked"] < 1.5:
        return _CACHE["xid"]

    best = None
    best_score = -1
    try:
        d = _display()
        for xid in _client_window_ids():
            try:
                win = d.create_resource_object("window", int(xid))
                title, wm_class = _window_text(win)
                score = _score_window(title, wm_class)
                if score > best_score:
                    best_score = score
                    best = int(xid)
            except Exception:
                continue
    except Exception:
        best = None

    # Fallback for Wine/WMs whose EWMH client list is unusual.
    if best is None or best_score < 5:
        xdotool = shutil.which("xdotool")
        if xdotool:
            queries = [
                [xdotool, "search", "--onlyvisible", "--name", "Dungeons & Dragons Online"],
                [xdotool, "search", "--onlyvisible", "--name", "Dungeons and Dragons Online"],
                [xdotool, "search", "--onlyvisible", "--class", "dndclient"],
                [xdotool, "search", "--onlyvisible", "--class", "dndclient64"],
            ]
            for cmd in queries:
                try:
                    out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, timeout=1.0)
                    vals = [x.strip() for x in out.splitlines() if x.strip().isdigit()]
                    if vals:
                        best = int(vals[-1])
                        best_score = 5
                        break
                except Exception:
                    pass

    _CACHE["xid"] = best if best_score >= 5 else None
    _CACHE["checked"] = now
    return _CACHE["xid"]


def active_window_id():
    try:
        d = _display()
        root = d.screen().root
        atom = d.intern_atom("_NET_ACTIVE_WINDOW", only_if_exists=True)
        if atom:
            from Xlib import X
            prop = root.get_full_property(atom, X.AnyPropertyType)
            if prop is not None and len(prop.value):
                return int(prop.value[0])
        focus = d.get_input_focus().focus
        return int(getattr(focus, "id", 0) or 0)
    except Exception:
        return None


def is_ddo_foreground():
    ddo = find_ddo_window()
    if not ddo:
        return False
    active = active_window_id()
    return bool(active and int(active) == int(ddo))


def focus_ddo():
    xid = find_ddo_window(force=True)
    if not xid:
        return False
    # wmctrl handles EWMH activation well on Cinnamon/Muffin and Wine windows.
    wmctrl = shutil.which("wmctrl")
    if wmctrl:
        try:
            subprocess.run(
                [wmctrl, "-ia", f"0x{xid:x}"],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            time.sleep(0.08)
            return True
        except Exception:
            pass
    xdotool = shutil.which("xdotool")
    if xdotool:
        try:
            subprocess.run(
                [xdotool, "windowactivate", "--sync", str(xid)],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.5,
            )
            time.sleep(0.08)
            return True
        except Exception:
            pass
    return False


def window_rect(xid=None):
    xid = xid or find_ddo_window()
    if not xid:
        return None
    try:
        d = _display()
        root = d.screen().root
        win = d.create_resource_object("window", int(xid))
        geom = win.get_geometry()
        tr = win.translate_coords(root, 0, 0)
        return (int(tr.x), int(tr.y), int(tr.x + geom.width), int(tr.y + geom.height))
    except Exception:
        pass
    xdotool = shutil.which("xdotool")
    if xdotool:
        try:
            out = subprocess.check_output(
                [xdotool, "getwindowgeometry", "--shell", str(xid)],
                text=True, stderr=subprocess.DEVNULL, timeout=1.0,
            )
            vals = {}
            for line in out.splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    vals[k.strip()] = v.strip()
            x, y = int(vals["X"]), int(vals["Y"])
            w, h = int(vals["WIDTH"]), int(vals["HEIGHT"])
            return (x, y, x + w, y + h)
        except Exception:
            pass
    return None


def client_rect_dict():
    r = window_rect()
    if not r:
        return None
    l, t, rr, bb = r
    return {"left": l, "top": t, "width": max(1, rr - l), "height": max(1, bb - t)}


def make_clickthrough(root):
    """Best-effort Linux overlay setup.

    Tk/X11 does not expose Windows-style WS_EX_TRANSPARENT.  Keep the overlay
    non-activating by behavior (it hides whenever DDO is not foreground) and
    use the translucent fallback already provided by live_overlay.py.
    """
    return False


def parent_is_alive(pid: int | None):
    if not pid:
        return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def linux_beep(repeat=1):
    """Best-effort desktop sound; falls back to the terminal/X bell."""
    repeat = max(1, int(repeat))
    sound_candidates = [
        Path("/usr/share/sounds/freedesktop/stereo/message.oga"),
        Path("/usr/share/sounds/freedesktop/stereo/complete.oga"),
    ]
    paplay = shutil.which("paplay")
    canberra = shutil.which("canberra-gtk-play")
    for i in range(repeat):
        played = False
        if paplay:
            for sound in sound_candidates:
                if sound.exists():
                    try:
                        subprocess.run([paplay, str(sound)], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=2.0)
                        played = True
                        break
                    except Exception:
                        pass
        if not played and canberra:
            try:
                subprocess.run([canberra, "-i", "message"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=2.0)
                played = True
            except Exception:
                pass
        if not played:
            try:
                print("\a", end="", flush=True)
            except Exception:
                pass
        if i + 1 < repeat:
            time.sleep(0.08)


class LinuxHotkeyManager:
    """X11 global hotkey active only while the DDO client is foreground.

    The key is passively grabbed only while DDO is active, so outside DDO it
    behaves normally. This build targets Cinnamon/X11, not Wayland.
    """

    def __init__(self, fired_event):
        self.fired_event = fired_event
        self.thread = None
        self.hotkey = None
        self.registered = False
        self.error = None
        self._stop = threading.Event()
        self._ready = threading.Event()

    @staticmethod
    def parse(spec):
        try:
            from Xlib import X, XK
        except Exception as exc:
            raise RuntimeError("python-xlib is not installed. Run SETUP DDO Combat Analyzer.sh.") from exc
        parts = [x.strip() for x in str(spec).split("+") if x.strip()]
        mods = 0
        keysym_name = None
        for part in parts:
            low = part.lower()
            if low in ("ctrl", "control"):
                mods |= X.ControlMask
            elif low == "shift":
                mods |= X.ShiftMask
            elif low == "alt":
                mods |= X.Mod1Mask
            elif low in ("win", "windows", "super"):
                mods |= X.Mod4Mask
            elif low in ("pause", "break", "pause/break"):
                keysym_name = "Pause"
            elif low.startswith("f") and low[1:].isdigit() and 1 <= int(low[1:]) <= 24:
                keysym_name = low.upper()
        if not keysym_name:
            raise ValueError("Choose Pause/Break or an F-key hotkey (F1-F24).")
        keysym = XK.string_to_keysym(keysym_name)
        if not keysym:
            raise ValueError(f"X11 could not resolve key {keysym_name}.")
        return mods, keysym

    def start(self, spec):
        self.stop()
        _require_x11()
        self.hotkey = spec
        mods, keysym = self.parse(spec)
        self._stop.clear()
        self._ready.clear()
        self.error = None

        def worker():
            grabbed = False
            d = None
            root = None
            keycode = None
            grab_mods = []
            try:
                from Xlib import X, display
                import select
                d = display.Display()
                root = d.screen().root
                keycode = d.keysym_to_keycode(keysym)
                if not keycode:
                    raise RuntimeError("X11 could not map the selected hotkey to a keycode.")
                # Grab with common NumLock/CapsLock combinations as well.
                variants = (0, X.LockMask, X.Mod2Mask, X.LockMask | X.Mod2Mask)
                grab_mods = sorted(set(mods | v for v in variants))

                def set_grab(want):
                    nonlocal grabbed
                    if want == grabbed:
                        return
                    if want:
                        for m in grab_mods:
                            root.grab_key(keycode, m, True, X.GrabModeAsync, X.GrabModeAsync)
                    else:
                        for m in grab_mods:
                            try:
                                root.ungrab_key(keycode, m)
                            except Exception:
                                pass
                    d.sync()
                    grabbed = want

                self.registered = True
                self._ready.set()
                last_fire = 0.0
                while not self._stop.is_set():
                    try:
                        want = is_ddo_foreground()
                        set_grab(want)
                    except Exception:
                        pass

                    r, _, _ = select.select([d.fileno()], [], [], 0.10)
                    if not r:
                        continue
                    while d.pending_events():
                        ev = d.next_event()
                        if ev.type == X.KeyPress and int(getattr(ev, "detail", -1)) == int(keycode):
                            # Ignore lock masks when comparing modifiers.
                            state = int(getattr(ev, "state", 0)) & ~(X.LockMask | X.Mod2Mask)
                            if state == mods and is_ddo_foreground():
                                now = time.monotonic()
                                if now - last_fire > 0.45:
                                    last_fire = now
                                    self.fired_event.set()
                set_grab(False)
            except Exception as exc:
                self.error = str(exc)
                self.registered = False
                self._ready.set()
            finally:
                if d is not None:
                    try:
                        if grabbed and root is not None and keycode:
                            for m in grab_mods:
                                root.ungrab_key(keycode, m)
                            d.sync()
                    except Exception:
                        pass
                    try:
                        d.close()
                    except Exception:
                        pass
                self.registered = False

        self.thread = threading.Thread(target=worker, name="x11-hotkey", daemon=True)
        self.thread.start()
        self._ready.wait(timeout=1.5)
        if not self.registered:
            raise RuntimeError(self.error or "X11 hotkey could not be installed.")

    def stop(self):
        old = self.thread
        self._stop.set()
        if old is not None and old.is_alive() and old is not threading.current_thread():
            old.join(timeout=0.8)
        self.thread = None
        self.registered = False
