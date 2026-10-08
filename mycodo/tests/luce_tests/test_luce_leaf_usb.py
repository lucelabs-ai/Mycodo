# coding=utf-8
"""Tests for devices/luce_leaf_usb.py. No Pi, camera or OpenCV needed.

Run: python -m pytest -q mycodo/tests/luce_tests
"""
import errno
import json
import os
import struct
import threading
import time

import pytest

from mycodo.devices.luce_leaf_usb import LEAF_USB_DEFAULT_CAPTURE_DELAY_SEC
from mycodo.devices.luce_leaf_usb import LEAF_USB_MAX_CAPTURE_DELAY_SEC
from mycodo.devices.luce_leaf_usb import LEAF_USB_PORT_MAP
from mycodo.devices.luce_leaf_usb import LEAF_USB_V4L2_CONTROLS
from mycodo.devices.luce_leaf_usb import LeafUsbError
from mycodo.devices.luce_leaf_usb import leaf_usb_apply_controls
from mycodo.devices.luce_leaf_usb import leaf_usb_capture
from mycodo.devices.luce_leaf_usb import leaf_usb_capture_lock
from mycodo.devices.luce_leaf_usb import leaf_usb_device_path
from mycodo.devices.luce_leaf_usb import leaf_usb_page_info
from mycodo.devices.luce_leaf_usb import leaf_usb_query_controls
from mycodo.devices.luce_leaf_usb import leaf_usb_read_frame
from mycodo.devices.luce_leaf_usb import leaf_usb_settings
from mycodo.devices.luce_leaf_usb import leaf_usb_settings_from_form
from mycodo.devices.luce_leaf_usb import leaf_usb_settings_json
from mycodo.devices.luce_leaf_usb import leaf_usb_settle

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


# leaf_usb_settle (the capture delay)

class FakeClock:
    """Time that moves only when a frame is read: each read takes `per_read` seconds."""

    def __init__(self, per_read=0.1):
        self.now, self.per_read = 0.0, per_read

    def __call__(self):
        return self.now


class StreamingCapture(FakeCapture):
    def __init__(self, clock, *script):
        super().__init__(*script)
        self.clock = clock

    def read(self):
        self.clock.now += self.clock.per_read
        return super().read()


def test_the_delay_streams_and_discards_frames_then_keeps_the_next():
    clock = FakeClock(per_read=0.25)
    cap = StreamingCapture(clock, *[(True, 'discarded')] * 12, (True, 'kept'))
    assert leaf_usb_settle(cap, (EmptyFrameError,), 3.0, clock=clock) == (True, 'kept')
    assert cap.reads == 13  # 12 reads fill the 3 s, then the next one is kept
    assert clock.now == 3.25


def test_empty_frames_during_the_delay_are_ignored():
    clock = FakeClock(per_read=0.5)
    cap = StreamingCapture(clock, EmptyFrameError('!buf.empty()'), (True, 'x'), (True, 'kept'))
    assert leaf_usb_settle(cap, (EmptyFrameError,), 1.0, clock=clock) == (True, 'kept')


def test_no_delay_reads_the_frame_straight_away():
    clock = FakeClock()
    cap = StreamingCapture(clock, (True, 'kept'))
    assert leaf_usb_settle(cap, (EmptyFrameError,), 0, clock=clock) == (True, 'kept')
    assert cap.reads == 1


def test_a_cancelled_capture_stops_streaming():
    cancelled = threading.Event()
    cancelled.set()
    clock = FakeClock()
    cap = StreamingCapture(clock, (True, 'x'))
    assert leaf_usb_settle(cap, (EmptyFrameError,), 3.0, cancelled=cancelled, clock=clock) == (False, None)
    assert cap.reads == 0


@needs_flock
def test_giving_up_on_a_capture_cancels_it(tmp_path):
    cancelled = threading.Event()
    release = threading.Event()
    with pytest.raises(LeafUsbError, match="abandoned"):
        capture('dev-cancel', lambda: release.wait(5), tmp_path, timeout_sec=0.2, cancelled=cancelled)
    assert cancelled.is_set()
    release.set()


# V4L2 controls, against a camera emulated at the ioctl level

CID = {name: cid for name, cid, _ in LEAF_USB_V4L2_CONTROLS}
QUERYCTRL = struct.Struct('<II32siiiiI8x')
QUERYMENU = struct.Struct('<II32sI')
CONTROL = struct.Struct('<Ii')
INTEGER, BOOLEAN, MENU = 1, 2, 3


class FakeUvcCamera:
    """The controls of a typical UVC webcam, answering VIDIOC_QUERYCTRL, _QUERYMENU, _G_CTRL and _S_CTRL.

    As the real driver does: exposure_time_absolute is inactive unless auto_exposure is Manual (1), and
    white_balance_temperature is inactive while white_balance_automatic is on; setting an inactive
    control is refused.
    """

    def __init__(self, **overrides):
        self.controls = {
            'auto_exposure': dict(type=MENU, min=0, max=3, step=1, default=3, label='Auto Exposure',
                                  menu={1: 'Manual Mode', 3: 'Aperture Priority Mode'}),
            'exposure_dynamic_framerate': dict(type=BOOLEAN, min=0, max=1, step=1, default=0),
            'white_balance_automatic': dict(type=BOOLEAN, min=0, max=1, step=1, default=1),
            'brightness': dict(type=INTEGER, min=-64, max=64, step=1, default=0),
            'contrast': dict(type=INTEGER, min=0, max=95, step=1, default=32),
            'saturation': dict(type=INTEGER, min=0, max=100, step=1, default=55),
            'gain': dict(type=INTEGER, min=0, max=100, step=1, default=0),
            'power_line_frequency': dict(type=MENU, min=0, max=2, step=1, default=1,
                                         menu={0: 'Disabled', 1: '50 Hz', 2: '60 Hz'}),
            'white_balance_temperature': dict(type=INTEGER, min=2800, max=6500, step=1, default=4600),
            'exposure_time_absolute': dict(type=INTEGER, min=1, max=5000, step=1, default=157),
        }
        self.controls.update(overrides)
        self.values = {name: c['default'] for name, c in self.controls.items()}
        self.set_calls = []
        self.by_cid = {CID[name]: name for name in self.controls}

    def inactive(self, name):
        if name == 'exposure_time_absolute':
            return self.values.get('auto_exposure') != 1
        if name == 'white_balance_temperature':
            return self.values.get('white_balance_automatic') == 1
        return False

    def ioctl(self, fd, request, buf, mutate):
        if request == 0xC0445624:  # VIDIOC_QUERYCTRL
            cid = QUERYCTRL.unpack(buf)[0]
            name = self.by_cid.get(cid)
            if name is None:
                raise OSError(errno.EINVAL, 'Invalid argument')
            c = self.controls[name]
            flags = (0x10 if self.inactive(name) else 0) | (0x04 if c.get('read_only') else 0)
            buf[:] = QUERYCTRL.pack(cid, c['type'], c.get('label', name.title()).encode(),
                                    c['min'], c['max'], c['step'], c['default'], flags)
        elif request == 0xC02C5625:  # VIDIOC_QUERYMENU
            cid, index, _, _ = QUERYMENU.unpack(buf)
            label = self.controls[self.by_cid[cid]]['menu'].get(index)
            if label is None:
                raise OSError(errno.EINVAL, 'Invalid argument')
            buf[:] = QUERYMENU.pack(cid, index, label.encode(), 0)
        elif request == 0xC008561B:  # VIDIOC_G_CTRL
            cid = CONTROL.unpack(buf)[0]
            buf[:] = CONTROL.pack(cid, self.values[self.by_cid[cid]])
        elif request == 0xC008561C:  # VIDIOC_S_CTRL
            cid, value = CONTROL.unpack(buf)
            name = self.by_cid[cid]
            if self.inactive(name):
                raise OSError(errno.EACCES, 'Permission denied')
            self.values[name] = value
            self.set_calls.append((name, value))
        else:
            raise AssertionError(hex(request))

    def query(self):
        return leaf_usb_query_controls('/dev/fake', ioctl=self.ioctl, opener=lambda path: 3)

    def apply(self, values):
        return leaf_usb_apply_controls('/dev/fake', values, ioctl=self.ioctl, opener=lambda path: 3)


def test_the_controls_a_camera_has_are_read_with_its_own_defaults_and_ranges():
    found = FakeUvcCamera().query()
    assert set(found) == set(FakeUvcCamera().controls)  # hue, gamma, focus... it lacks are left out
    assert found['brightness'] == dict(type=INTEGER, label='Brightness', min=-64, max=64, step=1,
                                       default=0, read_only=False, inactive=False)
    assert found['power_line_frequency']['menu'] == {0: 'Disabled', 1: '50 Hz', 2: '60 Hz'}
    assert found['auto_exposure']['menu'] == {1: 'Manual Mode', 3: 'Aperture Priority Mode'}
    assert found['exposure_time_absolute']['inactive'] is True


def test_a_setting_left_empty_goes_back_to_the_camera_default():
    camera = FakeUvcCamera()
    camera.values.update(brightness=40, saturation=90)  # left over from an earlier setting
    assert camera.apply({}) == []
    assert camera.values['brightness'] == 0 and camera.values['saturation'] == 55


def test_a_chosen_value_is_set_and_one_already_right_is_not_set_again():
    camera = FakeUvcCamera()
    assert camera.apply({'contrast': 50, 'gain': 0}) == []
    assert camera.values['contrast'] == 50
    assert ('gain', 0) not in camera.set_calls  # already at 0


def test_manual_exposure_is_applied_in_the_same_pass_as_its_mode():
    camera = FakeUvcCamera()
    assert camera.apply({'auto_exposure': 1, 'exposure_time_absolute': 300}) == []
    assert camera.values['auto_exposure'] == 1 and camera.values['exposure_time_absolute'] == 300
    assert [n for n, _ in camera.set_calls] == ['auto_exposure', 'exposure_time_absolute']


def test_a_manual_value_while_its_automatic_mode_is_on_is_reported_not_forced():
    camera = FakeUvcCamera()
    problems = camera.apply({'exposure_time_absolute': 300})  # auto_exposure stays at its default, 3
    assert problems == ['exposure_time_absolute: 300 ignored while its automatic mode is on']
    assert camera.values['exposure_time_absolute'] == 157


def test_an_out_of_range_value_is_reported_and_left_unchanged():
    camera = FakeUvcCamera()
    problems = camera.apply({'brightness': 500})
    assert problems == ['brightness: 500 is outside this camera\'s range -64..64; left unchanged']
    assert camera.values['brightness'] == 0


def test_a_control_the_camera_lacks_is_reported_only_when_it_was_chosen():
    camera = FakeUvcCamera()
    assert camera.apply({}) == []
    assert camera.apply({'focus_absolute': 10}) == ['focus_absolute: this camera has no such control; 10 ignored']


def test_a_read_only_control_is_left_alone():
    camera = FakeUvcCamera(gain=dict(type=INTEGER, min=0, max=100, step=1, default=0, read_only=True))
    camera.values['gain'] = 7
    assert camera.apply({}) == []
    assert camera.values['gain'] == 7


def test_the_control_list_uses_v4l2s_own_names_and_ids():
    assert ('brightness', 0x00980900) in [(n, c) for n, c, _ in LEAF_USB_V4L2_CONTROLS]
    assert ('exposure_time_absolute', 0x009a0902) in [(n, c) for n, c, _ in LEAF_USB_V4L2_CONTROLS]
    names = [n for n, _, _ in LEAF_USB_V4L2_CONTROLS]
    # Each automatic mode before the value it governs.
    assert names.index('auto_exposure') < names.index('exposure_time_absolute')
    assert names.index('white_balance_automatic') < names.index('white_balance_temperature')
    assert names.index('focus_automatic_continuous') < names.index('focus_absolute')


# Settings: custom_options JSON, the old columns, and the Camera page's form

def test_settings_round_trip_through_custom_options():
    stored = leaf_usb_settings_json(4.5, {'brightness': 10, 'auto_exposure': 1})
    assert leaf_usb_settings(stored) == (4.5, {'brightness': 10, 'auto_exposure': 1})


def test_a_new_camera_has_the_default_delay_and_every_control_at_its_default():
    assert leaf_usb_settings(leaf_usb_settings_json(LEAF_USB_DEFAULT_CAPTURE_DELAY_SEC, {})) == (3.0, {})


def test_a_camera_saved_before_these_settings_keeps_what_it_had():
    legacy = {'brightness': 120, 'contrast': -1, 'saturation': None, 'gain': 0, 'exposure': 250}
    assert leaf_usb_settings('', legacy) == (3.0, {
        'brightness': 120, 'gain': 0, 'auto_exposure': 1, 'exposure_time_absolute': 250})
    assert leaf_usb_settings('', {'brightness': -1, 'exposure': None}) == (3.0, {})


def test_unreadable_settings_fall_back_instead_of_breaking_the_capture():
    assert leaf_usb_settings('{not json') == (3.0, {})
    assert leaf_usb_settings(json.dumps({'capture_delay_s': 'x', 'v4l2': {'brightness': 'y', 'nonsense': 1}})) == (3.0, {})
    assert leaf_usb_settings(json.dumps({'capture_delay_s': 99}))[0] == LEAF_USB_MAX_CAPTURE_DELAY_SEC


def test_the_form_stores_chosen_values_and_leaves_empty_ones_at_default():
    form = {'leaf_usb_capture_delay': '2.5', 'leaf_usb_v4l2_brightness': '-10',
            'leaf_usb_v4l2_contrast': '', 'leaf_usb_v4l2_power_line_frequency': '2'}
    stored, errors = leaf_usb_settings_from_form(form, FakeUvcCamera().query())
    assert errors == []
    assert leaf_usb_settings(stored) == (2.5, {'brightness': -10, 'power_line_frequency': 2})


def test_the_form_refuses_bad_values():
    form = {'leaf_usb_capture_delay': '30', 'leaf_usb_v4l2_brightness': '500',
            'leaf_usb_v4l2_gain': 'lots'}
    _, errors = leaf_usb_settings_from_form(form, FakeUvcCamera().query())
    assert errors == ['Capture delay must be a number of seconds from 0 to 10',
                      'Brightness must be from -64 to 64 on this camera',
                      'Gain must be a whole number, or empty for the camera default']


def test_the_form_without_the_camera_checks_numbers_only():
    stored, errors = leaf_usb_settings_from_form({'leaf_usb_v4l2_brightness': '500'}, None)
    assert errors == [] and leaf_usb_settings(stored) == (3.0, {'brightness': 500})


def test_the_page_shows_a_camera_it_cannot_reach_with_a_reason():
    info = leaf_usb_page_info('not-a-port', leaf_usb_settings_json(3, {'gain': 5}))
    assert info['controls'] is None and 'USB port must be one of' in info['note']
    assert info['values'] == {'gain': 5} and info['delay'] == 3.0

