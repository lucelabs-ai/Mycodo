# coding=utf-8
"""Tests for devices/luce_leaf_usb.py. No Pi, camera or OpenCV needed.

Run: python -m pytest -q mycodo/tests/luce_tests
"""
import os
import threading
import time

import pytest

from mycodo.devices.luce_leaf_usb import LEAF_USB_PORT_MAP
from mycodo.devices.luce_leaf_usb import LeafUsbError
from mycodo.devices.luce_leaf_usb import leaf_usb_capture
from mycodo.devices.luce_leaf_usb import leaf_usb_capture_lock
from mycodo.devices.luce_leaf_usb import leaf_usb_device_path
from mycodo.devices.luce_leaf_usb import leaf_usb_read_frame

PI4 = 'platform-fd500000.pcie-pci-0000:01:00.0'
PI5 = 'platform-xhci-hcd.0'


def by_path(tmp_path, *names):
    """A fake /dev/v4l/by-path holding the given node names."""
    for name in names:
        (tmp_path / name).touch()
    return str(tmp_path)


def node(controller, topology, index=0):
    return f"{controller}-usb-0:{topology}:1.0-video-index{index}"


# leaf_usb_device_path

# By-path node names contain ':', which a Windows file name cannot.
needs_posix_names = pytest.mark.skipif(
    os.name != 'posix', reason="by-path names need ':' in file names; the Pi allows it")

@needs_posix_names
def test_resolves_a_port_on_the_pi_4_controller(tmp_path):
    d = by_path(tmp_path, node(PI4, '1.3'))
    assert leaf_usb_device_path('3', d) == os.path.join(d, node(PI4, '1.3'))


@needs_posix_names
def test_resolves_the_same_port_on_another_controller(tmp_path):
    d = by_path(tmp_path, node(PI5, '1.2.4.4'))
    assert leaf_usb_device_path('H10', d) == os.path.join(d, node(PI5, '1.2.4.4'))


@needs_posix_names
def test_every_port_in_the_map_resolves_to_its_own_node(tmp_path):
    d = by_path(tmp_path, *(node(PI4, t) for t in LEAF_USB_PORT_MAP.values()))
    for port, topology in LEAF_USB_PORT_MAP.items():
        assert leaf_usb_device_path(port, d) == os.path.join(d, node(PI4, topology))


@needs_posix_names
def test_a_port_does_not_match_a_camera_further_down_its_hub(tmp_path):
    # Port 2 (1.2) is where the second hub plugs in; its cameras (1.2.x)
    # must not be taken for a camera on port 2 itself.
    d = by_path(tmp_path, node(PI4, '1.2.3'), node(PI4, '1.2.1.3'))
    with pytest.raises(LeafUsbError, match="No camera on USB port 2"):
        leaf_usb_device_path('2', d)


@needs_posix_names
def test_the_metadata_node_is_not_a_camera(tmp_path):
    d = by_path(tmp_path, node(PI4, '1.1', index=1))
    with pytest.raises(LeafUsbError, match="No camera on USB port 1"):
        leaf_usb_device_path('1', d)


@needs_posix_names
def test_the_port_is_stripped_and_may_be_given_as_a_number(tmp_path):
    d = by_path(tmp_path, node(PI4, '1.4'))
    assert leaf_usb_device_path(' 4 ', d) == os.path.join(d, node(PI4, '1.4'))
    assert leaf_usb_device_path(4, d) == os.path.join(d, node(PI4, '1.4'))


def test_an_unknown_port_names_the_valid_ones(tmp_path):
    with pytest.raises(LeafUsbError, match=r"must be one of \['1', '2'"):
        leaf_usb_device_path('/dev/video0', str(tmp_path))


def test_an_empty_port_is_an_error_not_a_wrong_path(tmp_path):
    with pytest.raises(LeafUsbError, match="No camera on USB port H1"):
        leaf_usb_device_path('H1', str(tmp_path))


@needs_posix_names
def test_two_controllers_with_the_same_topology_is_an_error(tmp_path):
    d = by_path(tmp_path, node(PI5, '1.1'), node('platform-xhci-hcd.1', '1.1'))
    with pytest.raises(LeafUsbError, match="more than one camera"):
        leaf_usb_device_path('1', d)


# leaf_usb_capture and leaf_usb_capture_lock

needs_flock = pytest.mark.skipif(
    os.name != 'posix', reason="flock() is POSIX-only; the Pi has it")


def capture(key, func, tmp_path, **kwargs):
    return leaf_usb_capture(key, func, lock_file=str(tmp_path / 'leaf_usb.lock'), **kwargs)


class Wedge:
    """A capture that blocks until released, recording when it ran."""

    def __init__(self):
        self.release = threading.Event()
        self.returned = threading.Event()

    def __call__(self):
        self.release.wait(10)
        self.returned.set()
        return True, 'late frame'


def wait_until_free(key, tmp_path):
    """The abandoned thread clears its key just after returning."""
    deadline = time.monotonic() + 5
    while True:
        try:
            return capture(key, lambda: (True, 'frame'), tmp_path)
        except LeafUsbError:
            assert time.monotonic() < deadline
            time.sleep(0.01)


@needs_flock
def test_returns_what_the_capture_returns(tmp_path):
    assert capture('dev-ok', lambda: (True, 'frame'), tmp_path) == (True, 'frame')


@needs_flock
def test_raises_what_the_capture_raises(tmp_path):
    def fail():
        raise LeafUsbError("Could not open camera")
    with pytest.raises(LeafUsbError, match="Could not open camera"):
        capture('dev-fail', fail, tmp_path)


@needs_flock
def test_a_wedged_capture_is_abandoned_and_its_device_refused_until_it_returns(tmp_path):
    wedged = Wedge()
    started = time.monotonic()
    with pytest.raises(LeafUsbError, match="did not finish within 0.2 s"):
        capture('dev-wedged', wedged, tmp_path, timeout_sec=0.2)
    assert time.monotonic() - started < 2

    # Still stuck: the same device is refused at once, not stacked.
    with pytest.raises(LeafUsbError, match="has not returned yet"):
        capture('dev-wedged', lambda: (True, 'frame'), tmp_path)

    wedged.release.set()
    assert wedged.returned.wait(5)
    assert wait_until_free('dev-wedged', tmp_path) == (True, 'frame')


@needs_flock
def test_an_abandoned_capture_keeps_the_lock_until_it_really_returns(tmp_path):
    # The overlap the lock exists to prevent: camera A wedges, the caller
    # gives up on it, and camera B must not capture while A's call is still
    # running on the shared USB controller.
    wedged = Wedge()
    with pytest.raises(LeafUsbError, match="abandoned"):
        capture('dev-a', wedged, tmp_path, timeout_sec=0.2)

    b_ran = threading.Event()

    def camera_b():
        b_ran.set()
        return True, 'frame'

    with pytest.raises(LeafUsbError, match="held the camera lock"):
        capture('dev-b', camera_b, tmp_path, lock_wait_sec=0.3)
    assert not b_ran.is_set()

    # Once A's call returns, the lock is free and B captures.
    wedged.release.set()
    assert wedged.returned.wait(5)
    assert capture('dev-b', camera_b, tmp_path) == (True, 'frame')
    assert b_ran.is_set()


@needs_flock
def test_waiting_for_the_lock_does_not_use_up_the_capture_time(tmp_path):
    lock_file = str(tmp_path / 'leaf_usb.lock')
    holder = threading.Event()

    def hold_briefly():
        with leaf_usb_capture_lock(lock_file):
            holder.set()
            time.sleep(0.5)

    threading.Thread(target=hold_briefly, daemon=True).start()
    assert holder.wait(5)

    def slow_capture():
        time.sleep(0.3)
        return True, 'frame'

    # 0.5 s waiting plus 0.3 s capturing, with a 0.4 s capture limit: fine,
    # because the limit starts when the lock is taken.
    assert capture('dev-slow', slow_capture, tmp_path, timeout_sec=0.4) == (True, 'frame')


HOLD_LOCK_IN_ANOTHER_PROCESS = """
import sys
from mycodo.devices.luce_leaf_usb import leaf_usb_capture_lock
with leaf_usb_capture_lock(sys.argv[1]):
    print('held', flush=True)
    sys.stdin.read()  # until the test closes our stdin
"""


def acquire_in_thread(lock_file):
    """Start a thread that takes the lock; return the event it sets then."""
    acquired = threading.Event()

    def take():
        with leaf_usb_capture_lock(lock_file):
            acquired.set()

    threading.Thread(target=take, daemon=True).start()
    return acquired


@needs_flock
def test_a_capture_waits_for_another_process_holding_the_lock(tmp_path):
    import subprocess
    import sys

    lock_file = str(tmp_path / 'leaf_usb.lock')
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..'))
    other = subprocess.Popen(
        [sys.executable, '-c', HOLD_LOCK_IN_ANOTHER_PROCESS, lock_file],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        env=dict(os.environ, PYTHONPATH=repo_root))
    try:
        assert other.stdout.readline().strip() == 'held'
        acquired = acquire_in_thread(lock_file)
        assert not acquired.wait(0.5)
        other.stdin.close()
        assert acquired.wait(5)
    finally:
        other.kill()
        other.wait()


@needs_flock
def test_a_capture_waits_for_another_thread_holding_the_lock(tmp_path):
    lock_file = str(tmp_path / 'leaf_usb.lock')
    with leaf_usb_capture_lock(lock_file):
        acquired = acquire_in_thread(lock_file)
        assert not acquired.wait(0.5)
    assert acquired.wait(5)


@needs_flock
def test_a_lock_never_released_is_given_up_on(tmp_path):
    lock_file = str(tmp_path / 'leaf_usb.lock')
    with leaf_usb_capture_lock(lock_file):
        started = time.monotonic()
        with pytest.raises(LeafUsbError, match="held the camera lock for over 0.2 s"):
            with leaf_usb_capture_lock(lock_file, wait_sec=0.2):
                pass
        assert time.monotonic() - started < 2


# leaf_usb_read_frame

class EmptyFrameError(Exception):
    """Stands in for cv2.error, which OpenCV 5 raises on an empty MJPG frame."""


class FakeCapture:
    """Plays back a script of reads: a frame, (False, None), or an exception."""

    def __init__(self, *script):
        self.script = list(script)
        self.reads = 0

    def read(self):
        self.reads += 1
        step = self.script.pop(0) if self.script else (False, None)
        if isinstance(step, Exception):
            raise step
        return step


def test_a_healthy_camera_gives_the_frame_after_the_warm_up_reads():
    cap = FakeCapture((True, 'w1'), (True, 'w2'), (True, 'frame'))
    assert leaf_usb_read_frame(cap, (EmptyFrameError,)) == (True, 'frame')
    assert cap.reads == 3


def test_an_empty_frame_while_warming_up_is_not_a_failed_capture():
    cap = FakeCapture(EmptyFrameError('!buf.empty()'), (True, 'w2'), (True, 'frame'))
    assert leaf_usb_read_frame(cap, (EmptyFrameError,)) == (True, 'frame')


def test_an_empty_frame_is_retried():
    cap = FakeCapture((True, 'w1'), (True, 'w2'), EmptyFrameError('!buf.empty()'),
                      (False, None), (True, 'frame'))
    assert leaf_usb_read_frame(cap, (EmptyFrameError,)) == (True, 'frame')
    assert cap.reads == 5


def test_a_camera_that_never_delivers_fails_after_the_attempts():
    cap = FakeCapture(*[EmptyFrameError('!buf.empty()')] * 10)
    assert leaf_usb_read_frame(cap, (EmptyFrameError,)) == (False, None)
    assert cap.reads == 5  # 2 warm-up + 3 attempts, then stop


def test_any_other_error_still_propagates():
    cap = FakeCapture((True, 'w1'), (True, 'w2'), RuntimeError('device gone'))
    with pytest.raises(RuntimeError, match="device gone"):
        leaf_usb_read_frame(cap, (EmptyFrameError,))

