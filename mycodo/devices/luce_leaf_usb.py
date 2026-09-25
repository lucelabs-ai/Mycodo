# coding=utf-8
"""Port resolution, capture lock and capture time limit for the 'leaf_usb'
camera library (the 'leaf_usb' branch of devices/camera.py).

Kept free of Mycodo, database and OpenCV imports so all three can be tested
without a Pi or a camera: mycodo/tests/luce_tests/test_luce_leaf_usb.py.
"""
import contextlib
import glob
import os
import threading

LEAF_USB_BY_PATH_DIR = '/dev/v4l/by-path'

# Shared by the daemon (timelapses) and the web UI (a "capture still" click),
# which are separate processes. Both run as root (install/*.service).
LEAF_USB_LOCK_FILE = '/var/lock/mycodo_leaf_usb.lock'

# A healthy open + three reads takes a second or two. Past this, the camera
# is treated as wedged and the capture is abandoned.
LEAF_USB_CAPTURE_TIMEOUT_SEC = 15

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
def leaf_usb_capture_lock(lock_file=LEAF_USB_LOCK_FILE):
    """Hold the one-capture-at-a-time lock, across processes.

    All ports share one upstream USB controller, so two captures at the same
    instant can corrupt each other's frames. A threading.Lock only covered
    one process, so a still from the web UI could overlap a timelapse from
    the daemon; flock() on a shared file serializes both. Each open() gets
    its own lock, so threads within one process are serialized too. Every
    holder is bounded by leaf_usb_run_bounded(), so waiting here is bounded.
    """
    import fcntl

    fd = os.open(lock_file, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the flock


_in_flight = set()
_in_flight_lock = threading.Lock()


def leaf_usb_run_bounded(key, func, timeout_sec=LEAF_USB_CAPTURE_TIMEOUT_SEC):
    """Return func(), or raise LeafUsbError if it takes over timeout_sec.

    A wedged UVC camera can block OpenCV's open or read in the kernel for a
    long time, and nothing in Python can interrupt that call. So func runs
    in a daemon thread that is abandoned on timeout: the caller gets an
    error, releases the capture lock, and the other cameras carry on -- one
    bad camera costs one missed frame instead of a stalled round.

    key (the device path) stays in flight until the abandoned call returns,
    and a capture on the same key is refused until then, rather than
    stacking another stuck thread on the same device.
    """
    with _in_flight_lock:
        if key in _in_flight:
            raise LeafUsbError(
                f"The previous capture on {key} has not returned yet")
        _in_flight.add(key)

    outcome = {}

    def worker():
        try:
            outcome['value'] = func()
        except Exception as err:
            outcome['error'] = err
        finally:
            with _in_flight_lock:
                _in_flight.discard(key)

    thread = threading.Thread(
        target=worker, name=f"leaf_usb {key}", daemon=True)
    thread.start()
    thread.join(timeout_sec)
    if thread.is_alive():
        raise LeafUsbError(
            f"Capture on {key} did not finish within {timeout_sec} s; "
            f"abandoned")
    if 'error' in outcome:
        raise outcome['error']
    return outcome.get('value')
