# coding=utf-8
"""Port resolution, capture lock, capture time limit and frame reads for the
'leaf_usb' camera library (the 'leaf_usb' branch of devices/camera.py).

Kept free of Mycodo, database and OpenCV imports so all of it can be tested
without a Pi or a camera: mycodo/tests/luce_tests/test_luce_leaf_usb.py.
"""
import contextlib
import glob
import json
import os
import struct
import threading
import time

LEAF_USB_BY_PATH_DIR = '/dev/v4l/by-path'

# Shared by the daemon (timelapses) and the web UI (a "capture still" click, or
# the Mycodo Bridge's /api/cameras/capture_image), which are separate
# processes. Both run as root (install/*.service). The same path the
# controllers' hand-patched camera.py already uses, so a Pi part-way between
# the two still serializes on one lock.
LEAF_USB_LOCK_FILE = '/var/lock/mycodo_leaf_usb_capture.lock'

# A healthy open + reads takes a few seconds. Past this, the camera is treated
# as wedged and the caller stops waiting for it (the capture lock stays held
# until the wedged call really returns -- see leaf_usb_capture).
LEAF_USB_CAPTURE_TIMEOUT_SEC = 15

# How long a capture waits for the lock before skipping its frame. A healthy
# holder releases within its capture delay plus a few seconds; a wedged one
# holds it until its kernel call returns, and this bounds what that costs the
# next camera.
LEAF_USB_LOCK_WAIT_SEC = 30

# How long a camera streams, discarding frames, before the frame that is kept:
# auto-exposure and auto white balance only adjust while the camera is
# streaming. Set per camera on the Camera page (leaf_usb_settings).
LEAF_USB_DEFAULT_CAPTURE_DELAY_SEC = 3.0
LEAF_USB_MAX_CAPTURE_DELAY_SEC = 10.0

# -----------------------------------------------------------------------
# USB port -> USB topology for 'leaf_usb' cameras.
#
# Each entry maps a Camera's selected "USB Port" to the physical USB
# topology of that port, exactly as reported by `v4l2-ctl --list-devices`
# or `ls -l /dev/v4l/by-path/` (the part between "usb-0:" and
# ":1.0-video-index0"). "video-index0" is the actual capture-capable node
# for these cameras (video-index1 is metadata-only).
#
# Ports '1'-'4' are the original 4-port hub, plugged directly into the
# Pi -- used directly when the second hub isn't plugged in.
# Ports 'H1'-'H10' are a second hub added to reach 12+ cameras per Pi,
# labeled with an 'H' prefix so they're visually distinct from the
# original 4 in the dropdown, numbered to match the second hub's own
# port labeling exactly. Its topology strings (1.2.X / 1.2.1.X /
# 1.2.4.X) show it's plugged into port '2' of the original hub, which
# means port '2' below no longer has a camera directly on it (selecting
# it will just correctly fail to find one, same as any other empty port).
# If that assumption is wrong -- if the second hub is actually plugged
# in somewhere else -- update the "1.2..." prefixes below to match
# wherever `v4l2-ctl --list-devices` actually shows it.
#
# Adding another hub or port later is just one more line here -- the
# Camera page's dropdown is built from these keys.
#
# The board's USB controller is deliberately not part of this map:
# leaf_usb_device_path() matches any controller, so the same map works on
# a Pi 4, a Pi 5 or a different kernel's naming.
# -----------------------------------------------------------------------
LEAF_USB_PORT_MAP = {
    # Original 4-port hub
    '1': '1.1',
    '2': '1.2',
    '3': '1.3',
    '4': '1.4',
    # Second hub (10 ports), 'H'-prefixed and numbered to match its own
    # port labeling exactly
    'H1': '1.2.3',
    'H2': '1.2.2',
    'H3': '1.2.1.3',
    'H4': '1.2.1.2',
    'H5': '1.2.1.1',
    'H6': '1.2.1.4',
    'H7': '1.2.4.3',
    'H8': '1.2.4.2',
    'H9': '1.2.4.1',
    'H10': '1.2.4.4',
}


class LeafUsbError(Exception):
    """A leaf_usb capture that cannot go ahead; the message is for the log."""


def leaf_usb_device_path(port, by_path_dir=LEAF_USB_BY_PATH_DIR):
    """Return the /dev/v4l/by-path node of the camera on a USB port.

    Matches on the topology alone, whatever the controller prefix before
    "-usb-0:" is. Raises LeafUsbError for a port not in LEAF_USB_PORT_MAP,
    for a port with no camera on it, and for a port that matches more than
    one node (a board with two USB controllers, where the topology alone
    doesn't say which one is meant).
    """
    port = str(port).strip()
    topology = LEAF_USB_PORT_MAP.get(port)
    if topology is None:
        raise LeafUsbError(
            f"USB port must be one of {list(LEAF_USB_PORT_MAP)}, got {port!r}")

    pattern = os.path.join(
        by_path_dir, f"*-usb-0:{topology}:1.0-video-index0")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise LeafUsbError(
            f"No camera on USB port {port} (nothing matches {pattern})")
    if len(matches) > 1:
        raise LeafUsbError(
            f"USB port {port} matches more than one camera: {matches}")
    return matches[0]


@contextlib.contextmanager
def leaf_usb_capture_lock(lock_file=LEAF_USB_LOCK_FILE, wait_sec=LEAF_USB_LOCK_WAIT_SEC):
    """Hold the one-capture-at-a-time lock, across processes.

    All ports share one upstream USB controller, so two captures at the same
    instant can corrupt each other's frames. A threading.Lock only covered
    one process, so a still from the web UI could overlap a timelapse from
    the daemon; flock() on a shared file serializes both. Each open() gets
    its own lock, so threads within one process are serialized too.

    Raises LeafUsbError if the lock isn't free within wait_sec. Captures take
    it through leaf_usb_capture(), which holds it for exactly as long as the
    capture really runs.
    """
    import fcntl

    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + wait_sec
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LeafUsbError(
                        f"Another leaf_usb capture held the camera lock for over "
                        f"{wait_sec:g} s; skipping this frame")
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # releases the flock


_in_flight = set()
_in_flight_lock = threading.Lock()


def leaf_usb_capture(key, func, timeout_sec=LEAF_USB_CAPTURE_TIMEOUT_SEC,
                     lock_file=LEAF_USB_LOCK_FILE, lock_wait_sec=LEAF_USB_LOCK_WAIT_SEC,
                     cancelled=None):
    """Return func() run under the capture lock, waiting at most timeout_sec.

    A wedged UVC camera can block OpenCV's open or read in the kernel for a
    long time, and nothing in Python can interrupt that call. So func runs
    in a daemon thread, and the caller stops waiting after timeout_sec and
    gets LeafUsbError -- one missed frame instead of a capture that never
    returns.

    The lock belongs to that thread, not to the caller: it is taken before
    func starts and released only when func actually returns. An abandoned
    capture can still be streaming from its camera, so releasing the lock
    when the caller gives up would let the next camera capture at the same
    time -- the overlap the lock exists to prevent. While a wedged call is
    still running, other captures wait up to lock_wait_sec and then skip
    their frame instead of joining it on the bus.

    key (the device path) stays in flight until the abandoned call returns,
    and a capture on the same key is refused until then, rather than
    stacking another stuck thread on the same device. timeout_sec counts
    from when the lock is taken, so waiting for it doesn't eat the
    capture's own time. `cancelled`, a threading.Event, is set when the
    caller gives up, so a func that loops (leaf_usb_settle) can stop early
    and hand the lock back sooner.
    """
    with _in_flight_lock:
        if key in _in_flight:
            raise LeafUsbError(
                f"The previous capture on {key} has not returned yet")
        _in_flight.add(key)

    outcome = {}
    locked = threading.Event()

    def worker():
        try:
            with leaf_usb_capture_lock(lock_file, lock_wait_sec):
                locked.set()
                outcome['value'] = func()
        except Exception as err:
            outcome['error'] = err
        finally:
            with _in_flight_lock:
                _in_flight.discard(key)

    thread = threading.Thread(
        target=worker, name=f"leaf_usb {key}", daemon=True)
    thread.start()
    # The worker gives up on the lock by itself after lock_wait_sec.
    locked.wait(lock_wait_sec + 1)
    thread.join(timeout_sec if locked.is_set() else 1)
    if thread.is_alive():
        if cancelled is not None:
            cancelled.set()
        if locked.is_set():
            raise LeafUsbError(
                f"Capture on {key} did not finish within {timeout_sec:g} s; "
                f"abandoned (the camera lock stays held until it returns)")
        raise LeafUsbError(f"Could not get the camera lock for {key}")
    if 'error' in outcome:
        raise outcome['error']
    return outcome.get('value')


def leaf_usb_read_frame(cap, read_errors, warmup=2, attempts=3):
    """Return (ok, frame) from an opened capture, tolerating empty frames.

    UVC cameras often deliver an empty frame or two right after opening or
    switching to MJPG. OpenCV 4 reported that as (False, None); OpenCV 5
    raises cv2.error from its MJPG decoder instead ("!buf.empty() in
    function 'imdecode_'"), which failed the whole capture on a throwaway
    warm-up read. read_errors is the exception type(s) to treat as "no
    frame this time" (devices/camera.py passes (cv2.error,)); any other
    exception still propagates.

    Discards `warmup` reads while exposure settles, then makes up to
    `attempts` reads for the frame itself.
    """
    for _ in range(warmup):
        try:
            cap.read()
        except read_errors:
            pass
    for _ in range(attempts):
        try:
            ok, frame = cap.read()
        except read_errors:
            continue
        if ok and frame is not None:
            return True, frame
    return False, None


def leaf_usb_settle(cap, read_errors, seconds, cancelled=None, clock=time.monotonic):
    """Stream for `seconds`, discarding every frame, then return (ok, frame).

    The capture delay. Auto-exposure and auto white balance only adjust while
    the camera is streaming (OpenCV starts the stream on the first read), so
    the camera reads and throws frames away for the whole delay rather than
    sitting idle, and the frame kept is the first one after it. Stops early
    if `cancelled` is set (the caller gave up: leaf_usb_capture).
    """
    deadline = clock() + max(0.0, seconds)
    while clock() < deadline:
        if cancelled is not None and cancelled.is_set():
            return False, None
        try:
            cap.read()
        except read_errors:
            pass
    if cancelled is not None and cancelled.is_set():
        return False, None
    return leaf_usb_read_frame(cap, read_errors, warmup=0)


# -----------------------------------------------------------------------
# V4L2 controls.
#
# Every image setting the leaf_usb library offers is a V4L2 control, set by
# its V4L2 control id on the camera's own device node -- not through
# OpenCV's CAP_PROP_* properties, whose names, scales and coverage differ
# from V4L2's. The names are the ones `v4l2-ctl --list-ctrls` prints, and a
# setting left empty is reset to the camera's own V4L2 default on every
# capture: V4L2 values persist in the camera between opens, so "default"
# has to be applied, not assumed.
#
# Order matters: each automatic mode comes before the value it governs, so
# that setting auto_exposure to Manual makes exposure_time_absolute
# settable in the same pass.
# -----------------------------------------------------------------------
LEAF_USB_V4L2_CONTROLS = [
    # (name, V4L2 control id, label shown when the camera can't be asked)
    ('auto_exposure', 0x009a0901, 'Auto Exposure'),
    ('exposure_dynamic_framerate', 0x009a0903, 'Exposure, Dynamic Framerate'),
    ('white_balance_automatic', 0x0098090c, 'White Balance, Automatic'),
    ('focus_automatic_continuous', 0x009a090c, 'Focus, Automatic Continuous'),
    ('brightness', 0x00980900, 'Brightness'),
    ('contrast', 0x00980901, 'Contrast'),
    ('saturation', 0x00980902, 'Saturation'),
    ('hue', 0x00980903, 'Hue'),
    ('gamma', 0x00980910, 'Gamma'),
    ('gain', 0x00980913, 'Gain'),
    ('sharpness', 0x0098091b, 'Sharpness'),
    ('backlight_compensation', 0x0098091c, 'Backlight Compensation'),
    ('power_line_frequency', 0x00980918, 'Power Line Frequency'),
    ('white_balance_temperature', 0x0098091a, 'White Balance Temperature'),
    ('exposure_time_absolute', 0x009a0902, 'Exposure Time, Absolute'),
    ('focus_absolute', 0x009a090a, 'Focus, Absolute'),
    ('zoom_absolute', 0x009a090d, 'Zoom, Absolute'),
    ('pan_absolute', 0x009a0908, 'Pan, Absolute'),
    ('tilt_absolute', 0x009a0909, 'Tilt, Absolute'),
]
LEAF_USB_V4L2_CONTROL_NAMES = [name for name, _, _ in LEAF_USB_V4L2_CONTROLS]

# linux/videodev2.h. The structs hold no pointers, so these are the same on
# 32- and 64-bit Raspberry Pi OS.
_VIDIOC_G_CTRL = 0xC008561B
_VIDIOC_S_CTRL = 0xC008561C
_VIDIOC_QUERYCTRL = 0xC0445624
_VIDIOC_QUERYMENU = 0xC02C5625
_QUERYCTRL = struct.Struct('<II32siiiiI8x')  # id, type, name, min, max, step, default, flags
_QUERYMENU = struct.Struct('<II32sI')        # id, index, name (or s64 value), reserved
_CONTROL = struct.Struct('<Ii')              # id, value

V4L2_CTRL_TYPE_INTEGER = 1
V4L2_CTRL_TYPE_BOOLEAN = 2
V4L2_CTRL_TYPE_MENU = 3
V4L2_CTRL_TYPE_INTEGER_MENU = 9
_FLAG_DISABLED = 0x0001
_FLAG_READ_ONLY = 0x0004
_FLAG_INACTIVE = 0x0010


class _V4L2Device:
    """A camera's device node, opened only for control ioctls.

    V4L2 allows this alongside OpenCV's own open: only streaming is exclusive,
    controls are not. `ioctl` and `opener` exist so the tests can stand in a
    fake camera.
    """

    def __init__(self, path, ioctl=None, opener=None):
        if ioctl is None:
            import fcntl
            ioctl = fcntl.ioctl
        self._ioctl = ioctl
        self._close = os.close if opener is None else (lambda fd: None)
        self.fd = (opener or (lambda p: os.open(p, os.O_RDWR | os.O_NONBLOCK)))(path)

    def close(self):
        self._close(self.fd)

    def query(self, cid):
        """This control's description, or None if the camera doesn't have it."""
        buf = bytearray(_QUERYCTRL.pack(cid, 0, b'', 0, 0, 0, 0, 0))
        try:
            self._ioctl(self.fd, _VIDIOC_QUERYCTRL, buf, True)
        except OSError:
            return None
        _, ctype, name, minimum, maximum, step, default, flags = _QUERYCTRL.unpack(buf)
        if flags & _FLAG_DISABLED:
            return None
        info = {
            'type': ctype, 'label': name.split(b'\0', 1)[0].decode('ascii', 'replace'),
            'min': minimum, 'max': maximum, 'step': step, 'default': default,
            'read_only': bool(flags & _FLAG_READ_ONLY), 'inactive': bool(flags & _FLAG_INACTIVE),
        }
        if ctype in (V4L2_CTRL_TYPE_MENU, V4L2_CTRL_TYPE_INTEGER_MENU):
            info['menu'] = self._menu(cid, ctype, minimum, maximum)
        return info

    def _menu(self, cid, ctype, minimum, maximum):
        items = {}
        for index in range(minimum, maximum + 1):
            buf = bytearray(_QUERYMENU.pack(cid, index, b'', 0))
            try:
                self._ioctl(self.fd, _VIDIOC_QUERYMENU, buf, True)
            except OSError:
                continue  # a gap in the menu is normal
            _, _, raw, _ = _QUERYMENU.unpack(buf)
            if ctype == V4L2_CTRL_TYPE_INTEGER_MENU:
                items[index] = str(struct.unpack_from('<q', raw)[0])
            else:
                items[index] = raw.split(b'\0', 1)[0].decode('ascii', 'replace')
        return items

    def get(self, cid):
        buf = bytearray(_CONTROL.pack(cid, 0))
        self._ioctl(self.fd, _VIDIOC_G_CTRL, buf, True)
        return _CONTROL.unpack(buf)[1]

    def set(self, cid, value):
        buf = bytearray(_CONTROL.pack(cid, int(value)))
        self._ioctl(self.fd, _VIDIOC_S_CTRL, buf, True)


def leaf_usb_query_controls(device_path, ioctl=None, opener=None):
    """{name: description} for the leaf_usb controls this camera has.

    A description has the camera's own label, type, min, max, step, default,
    whether it is read-only or currently inactive (governed by an automatic
    mode), and, for a menu, {value: label}. A control the camera lacks is
    left out.
    """
    device = _V4L2Device(device_path, ioctl, opener)
    try:
        found = {}
        for name, cid, _ in LEAF_USB_V4L2_CONTROLS:
            info = device.query(cid)
            if info is not None:
                found[name] = info
        return found
    finally:
        device.close()


def leaf_usb_apply_controls(device_path, values, ioctl=None, opener=None):
    """Set every leaf_usb control this camera has; return what couldn't be.

    `values` is {name: value} for the settings a person chose; every other
    control the camera has is set to the camera's own default. Returns a
    list of messages (an out-of-range value, a manual value ignored while
    its automatic mode is on, a control the camera refused) for the log --
    none of them stops the capture.
    """
    device = _V4L2Device(device_path, ioctl, opener)
    problems = []
    try:
        for name, cid, _ in LEAF_USB_V4L2_CONTROLS:
            # Asked again for each control: setting an automatic mode a step
            # earlier changes which of the later ones are inactive.
            info = device.query(cid)
            chosen = values.get(name)
            if info is None:
                if chosen is not None:
                    problems.append(f"{name}: this camera has no such control; {chosen} ignored")
                continue
            if info['read_only']:
                continue
            target = info['default'] if chosen is None else int(chosen)
            if not info['min'] <= target <= info['max']:
                problems.append(
                    f"{name}: {target} is outside this camera's range "
                    f"{info['min']}..{info['max']}; left unchanged")
                continue
            if info['inactive']:
                if chosen is not None:
                    problems.append(
                        f"{name}: {chosen} ignored while its automatic mode is on")
                continue
            try:
                if device.get(cid) != target:
                    device.set(cid, target)
            except OSError as err:
                problems.append(f"{name}: the camera refused {target} ({err.strerror or err})")
        return problems
    finally:
        device.close()


# -----------------------------------------------------------------------
# A camera's leaf_usb settings, kept as JSON in the Camera table's
# custom_options column (the other camera libraries use that column for
# their own command-line options; leaf_usb has none), so no migration:
#   {"capture_delay_s": 3.0, "v4l2": {"brightness": 140, "auto_exposure": 1}}
# A control not in "v4l2" is at the camera's default.
# -----------------------------------------------------------------------

def _clamp_delay(value):
    try:
        delay = float(value)
    except (TypeError, ValueError):
        return LEAF_USB_DEFAULT_CAPTURE_DELAY_SEC
    return min(max(delay, 0.0), LEAF_USB_MAX_CAPTURE_DELAY_SEC)


def leaf_usb_settings(custom_options, legacy=None):
    """(capture delay in seconds, {control name: value}) for one camera.

    `legacy` is the camera's old brightness/contrast/saturation/gain/exposure
    columns, used only for a camera saved before these settings existed
    (custom_options empty): -1 or None there meant "camera default", and an
    exposure meant manual exposure at that value.
    """
    if custom_options:
        try:
            data = json.loads(custom_options)
        except ValueError:
            data = None
        if isinstance(data, dict):
            controls = {}
            for name, value in (data.get('v4l2') or {}).items():
                if name in LEAF_USB_V4L2_CONTROL_NAMES and value is not None:
                    try:
                        controls[name] = int(value)
                    except (TypeError, ValueError):
                        pass
            return _clamp_delay(data.get('capture_delay_s', LEAF_USB_DEFAULT_CAPTURE_DELAY_SEC)), controls
    controls = {}
    for name in ('brightness', 'contrast', 'saturation', 'gain'):
        value = (legacy or {}).get(name)
        if value is not None and value >= 0:
            controls[name] = int(value)
    exposure = (legacy or {}).get('exposure')
    if exposure is not None:
        controls['auto_exposure'] = 1  # Manual Mode
        controls['exposure_time_absolute'] = int(exposure)
    return LEAF_USB_DEFAULT_CAPTURE_DELAY_SEC, controls


def leaf_usb_settings_json(capture_delay_s, controls):
    return json.dumps({
        'capture_delay_s': _clamp_delay(capture_delay_s),
        'v4l2': {name: int(controls[name]) for name in LEAF_USB_V4L2_CONTROL_NAMES if name in controls},
    })


def leaf_usb_page_info(port, custom_options, legacy=None, query=None):
    """What the Camera page shows for one leaf_usb camera.

    {'delay': seconds, 'values': {name: value}, 'controls': {name: description}
    or None, 'note': why the camera couldn't be asked, or ''}. Asking the
    camera is a handful of control ioctls on its node -- no streaming -- so it
    is safe while another process captures from it.
    """
    delay, values = leaf_usb_settings(custom_options, legacy)
    controls, note = None, ''
    try:
        controls = (query or leaf_usb_query_controls)(leaf_usb_device_path(port))
    except LeafUsbError as err:
        note = str(err)
    except OSError as err:
        note = f"Could not read this camera's controls: {err.strerror or err}"
    return {'delay': delay, 'values': values, 'controls': controls, 'note': note}


def leaf_usb_settings_from_form(form, queried=None):
    """(custom_options JSON, [errors]) from the Camera page's leaf_usb fields.

    `form` maps field names to strings: `leaf_usb_capture_delay`, and
    `leaf_usb_v4l2_<name>` per control, empty for "camera default".
    `queried` is leaf_usb_query_controls() for this camera if it could be
    asked, so a value outside its range is refused here rather than at the
    next capture.
    """
    errors = []
    raw_delay = (form.get('leaf_usb_capture_delay') or '').strip()
    try:
        delay = float(raw_delay) if raw_delay else LEAF_USB_DEFAULT_CAPTURE_DELAY_SEC
        if not 0 <= delay <= LEAF_USB_MAX_CAPTURE_DELAY_SEC:
            raise ValueError
    except ValueError:
        errors.append(f"Capture delay must be a number of seconds from 0 to "
                      f"{LEAF_USB_MAX_CAPTURE_DELAY_SEC:g}")
        delay = LEAF_USB_DEFAULT_CAPTURE_DELAY_SEC
    controls = {}
    for name, _, label in LEAF_USB_V4L2_CONTROLS:
        raw = (form.get(f'leaf_usb_v4l2_{name}') or '').strip()
        if not raw:
            continue
        try:
            value = int(raw)
        except ValueError:
            errors.append(f"{label} must be a whole number, or empty for the camera default")
            continue
        info = (queried or {}).get(name)
        if info and not info['min'] <= value <= info['max']:
            errors.append(f"{label} must be from {info['min']} to {info['max']} on this camera")
            continue
        controls[name] = value
    return leaf_usb_settings_json(delay, controls), errors
