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
from mycodo.devices.luce_leaf_usb import leaf_usb_capture_lock
from mycodo.devices.luce_leaf_usb import leaf_usb_device_path
from mycodo.devices.luce_leaf_usb import leaf_usb_run_bounded

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


# leaf_usb_run_bounded

def test_returns_what_the_capture_returns():
    assert leaf_usb_run_bounded('dev-ok', lambda: (True, 'frame')) == (True, 'frame')


def test_raises_what_the_capture_raises():
    def fail():
        raise LeafUsbError("Could not open camera")
    with pytest.raises(LeafUsbError, match="Could not open camera"):
        leaf_usb_run_bounded('dev-fail', fail)


def test_a_wedged_capture_is_abandoned_and_its_device_refused_until_it_returns():
    release = threading.Event()
    returned = threading.Event()

    def wedged():
        release.wait(10)
        returned.set()
        return True, 'late frame'

    started = time.monotonic()
    with pytest.raises(LeafUsbError, match="did not finish within 0.2 s"):
        leaf_usb_run_bounded('dev-wedged', wedged, timeout_sec=0.2)
    assert time.monotonic() - started < 2

    # Still stuck: the same device is refused at once, not stacked...
    with pytest.raises(LeafUsbError, match="has not returned yet"):
        leaf_usb_run_bounded('dev-wedged', lambda: (True, 'frame'))
    # ...while another camera captures normally.
    assert leaf_usb_run_bounded('dev-other', lambda: (True, 'frame')) == (True, 'frame')

    release.set()
    assert returned.wait(5)
    deadline = time.monotonic() + 5
    while True:
        try:
            assert leaf_usb_run_bounded('dev-wedged', lambda: (True, 'frame')) == (True, 'frame')
            break
        except LeafUsbError:
            # The abandoned thread clears its key just after returning.
            assert time.monotonic() < deadline
            time.sleep(0.01)


# leaf_usb_capture_lock

needs_flock = pytest.mark.skipif(
    os.name != 'posix', reason="flock() is POSIX-only; the Pi has it")

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
