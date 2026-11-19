# coding=utf-8
"""Tests for devices/luce_leaf_gantry_rgb.py. No gantry, network or Mycodo install needed.

Run: python -m pytest -q mycodo/tests/luce_tests
"""
import datetime
import json
import os

import pytest
import requests

from mycodo.devices.luce_leaf_gantry_rgb import LEAF_GANTRY_DEFAULT_MAX_AGE_MIN
from mycodo.devices.luce_leaf_gantry_rgb import LeafGantryError
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_base_url
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_check
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_fetch
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_page_info
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_photo_url
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_save
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_settings
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_settings_from_form
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_settings_json
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_stop_id

# A JPEG as a 4K UVC camera sends it: start and end markers around arbitrary bytes.
JPEG = b'\xff\xd8\xff\xe0' + bytes(range(256)) * 40 + b'\xff\xd9'
NOW = datetime.datetime(2026, 11, 19, 15, 0, 0, tzinfo=datetime.timezone.utc)


class FakeResponse:
    def __init__(self, status=200, content=b'', headers=None, body=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._body = body
        self.text = json.dumps(body) if body is not None else ''

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakeGet:
    """Stands in for requests.get and remembers what it was asked."""
    def __init__(self, response=None, raises=None):
        self.response = response
        self.raises = raises
        self.calls = []

    def __call__(self, url, headers=None, timeout=None):
        self.calls.append({'url': url, 'headers': headers, 'timeout': timeout})
        if self.raises:
            raise self.raises
        return self.response


def photo(age_s=120.0, captured_minutes_ago=None, **extra_headers):
    headers = {'Content-Type': 'image/jpeg', 'X-Capture-Id': 'r000042_20261119-145800_s03_rgb'}
    if age_s is not None:
        headers['X-Photo-Age-S'] = f"{age_s:.1f}"
    if captured_minutes_ago is not None:
        ts = NOW - datetime.timedelta(minutes=captured_minutes_ago)
        headers['X-Captured-At'] = ts.astimezone(
            datetime.timezone(datetime.timedelta(hours=-6))).isoformat(timespec='milliseconds')
    headers.update(extra_headers)
    return FakeResponse(200, JPEG, headers)


# -- addresses and stop ids ---------------------------------------------------------------

@pytest.mark.parametrize("typed, url", [
    ("192.168.1.50", "http://192.168.1.50:8080"),
    ("192.168.1.50:8090", "http://192.168.1.50:8090"),
    ("gantry-shelf1.local", "http://gantry-shelf1.local:8080"),
    (" http://gantry-shelf1.local:8080/ ", "http://gantry-shelf1.local:8080"),
    ("https://gantry.lan", "https://gantry.lan:8080"),
    ("[fd00::12]:8080", "http://[fd00::12]:8080"),
])
def test_gantry_address_forms(typed, url):
    assert leaf_gantry_base_url(typed) == url


@pytest.mark.parametrize("typed", ["", "   ", "http://gantry/api/v1", "gantry:port", "gantry:70000", "bad host"])
def test_bad_gantry_addresses_are_refused(typed):
    with pytest.raises(LeafGantryError):
        leaf_gantry_base_url(typed)


def test_stop_id_from_the_field_or_the_camera_name():
    assert leaf_gantry_stop_id('s07', 'anything') == 's07'
    assert leaf_gantry_stop_id('', 'gantry-s03') == 's03'
    assert leaf_gantry_stop_id(None, 'Gantry-s10') == 's10'
    with pytest.raises(LeafGantryError, match="No stop ID"):
        leaf_gantry_stop_id('', 'Shelf camera')
    with pytest.raises(LeafGantryError, match="not valid"):
        leaf_gantry_stop_id('S03', '')
    with pytest.raises(LeafGantryError, match="not valid"):
        leaf_gantry_stop_id('../s03', '')


def test_photo_url():
    assert leaf_gantry_photo_url('10.0.0.5', 's03') == 'http://10.0.0.5:8080/api/v1/stops/s03/latest'


# -- settings -----------------------------------------------------------------------

def test_settings_round_trip_and_defaults():
    assert leaf_gantry_settings(None) == ('', LEAF_GANTRY_DEFAULT_MAX_AGE_MIN)
    assert leaf_gantry_settings('not json') == ('', LEAF_GANTRY_DEFAULT_MAX_AGE_MIN)
    assert leaf_gantry_settings(leaf_gantry_settings_json('rg_abc', 20)) == ('rg_abc', 20.0)
    assert leaf_gantry_settings('{"max_age_min": -3}')[1] == LEAF_GANTRY_DEFAULT_MAX_AGE_MIN


def test_form_keeps_the_saved_token_when_left_empty():
    saved = leaf_gantry_settings_json('rg_saved', 15)
    options, errors = leaf_gantry_settings_from_form({'leaf_gantry_token': '', 'leaf_gantry_max_age': '12'}, saved)
    assert not errors and leaf_gantry_settings(options) == ('rg_saved', 12.0)
    options, errors = leaf_gantry_settings_from_form({'leaf_gantry_token': 'Bearer rg_new'}, saved)
    assert not errors and leaf_gantry_settings(options) == ('rg_new', 15.0)


def test_form_refuses_bad_values():
    _, errors = leaf_gantry_settings_from_form({'leaf_gantry_max_age': '0'})
    assert errors
    _, errors = leaf_gantry_settings_from_form({'leaf_gantry_max_age': 'soon'})
    assert errors
    _, errors = leaf_gantry_settings_from_form({'leaf_gantry_token': 'rg a b'})
    assert errors


def test_page_info_never_contains_the_token():
    info = leaf_gantry_page_info('', leaf_gantry_settings_json('rg_secret', 15), 'gantry-s02')
    assert info == {'stop': '', 'default_stop': 's02', 'max_age': 15.0, 'has_token': True}
    assert 'rg_secret' not in json.dumps(info)


# -- fetching a photo ----------------------------------------------------------------

def test_fresh_photo_is_returned_byte_for_byte_with_the_token_in_the_header():
    get = FakeGet(photo(age_s=300))
    data, info = leaf_gantry_fetch('gantry.local', 's03', 'rg_tok', 15, get=get, now=NOW)
    assert data == JPEG
    assert info['age_min'] == 5.0 and info['capture_id'].endswith('_s03_rgb')
    call = get.calls[0]
    assert call['url'] == 'http://gantry.local:8080/api/v1/stops/s03/latest'
    assert call['headers']['Authorization'] == 'Bearer rg_tok'
    assert 'rg_tok' not in call['url']
    assert sum(call['timeout']) < 15          # inside the bridge's 15 s per capture


def test_stale_photo_is_refused():
    with pytest.raises(LeafGantryError, match="17 min old"):
        leaf_gantry_fetch('gantry.local', 's03', 'rg', 15, get=FakeGet(photo(age_s=17 * 60)), now=NOW)


def test_age_falls_back_to_the_capture_time():
    get = FakeGet(photo(age_s=None, captured_minutes_ago=4))
    _, info = leaf_gantry_fetch('gantry.local', 's03', 'rg', 15, get=get, now=NOW)
    assert abs(info['age_min'] - 4) < 0.01
    with pytest.raises(LeafGantryError, match="20 min old"):
        leaf_gantry_fetch('gantry.local', 's03', 'rg', 15,
                          get=FakeGet(photo(age_s=None, captured_minutes_ago=20)), now=NOW)


def test_photo_without_any_time_is_refused():
    with pytest.raises(LeafGantryError, match="did not say when"):
        leaf_gantry_fetch('gantry.local', 's03', 'rg', 15, get=FakeGet(photo(age_s=None)), now=NOW)


def test_not_a_jpeg_is_refused():
    get = FakeGet(FakeResponse(200, b'<html>', {'Content-Type': 'text/html', 'X-Photo-Age-S': '1'}))
    with pytest.raises(LeafGantryError, match="did not send a JPEG"):
        leaf_gantry_fetch('gantry.local', 's03', 'rg', 15, get=get, now=NOW)


@pytest.mark.parametrize("status, body, message", [
    (404, {'error': 'not_found', 'message': "No stop 's03'"}, "not registered on the gantry"),
    (404, {'error': 'no_photo', 'message': "No photo yet"}, "no photo yet for stop s03"),
    (401, {'error': 'unauthorized', 'message': 'Invalid or revoked token'}, "rejected the access token"),
    (403, {'error': 'forbidden', 'message': 'needs the read scope'}, "refused the request"),
    (500, {'error': 'error', 'message': 'boom'}, "HTTP 500: boom"),
])
def test_gantry_answers_become_plain_causes(status, body, message):
    get = FakeGet(FakeResponse(status, b'', {'Content-Type': 'application/json'}, body))
    with pytest.raises(LeafGantryError, match=message):
        leaf_gantry_fetch('gantry.local', 's03', 'rg', 15, get=get, now=NOW)


def test_unreachable_and_slow_gantries():
    with pytest.raises(LeafGantryError, match="unreachable"):
        leaf_gantry_fetch('gantry.local', 's03', 'rg', 15,
                          get=FakeGet(raises=requests.ConnectionError("refused")), now=NOW)
    with pytest.raises(LeafGantryError, match="did not answer within"):
        leaf_gantry_fetch('gantry.local', 's03', 'rg', 15,
                          get=FakeGet(raises=requests.ReadTimeout("slow")), now=NOW)


# -- saving and the check on save --------------------------------------------------------

def test_save_writes_exactly_the_bytes_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / 'Still-1-gantry-s03.jpg'
    target.write_bytes(b'old')
    leaf_gantry_save(JPEG, str(target))
    assert target.read_bytes() == JPEG
    assert sorted(os.listdir(tmp_path)) == ['Still-1-gantry-s03.jpg']
    assert oct(os.stat(target).st_mode & 0o777) == '0o644'


def test_check_on_save():
    ok = FakeGet(FakeResponse(200, b'{}', {}, {'id': 's03'}))
    assert leaf_gantry_check('gantry.local', 's03', 'rg', get=ok) == ''
    assert ok.calls[0]['url'] == 'http://gantry.local:8080/api/v1/stops/s03'
    missing = FakeGet(FakeResponse(404, b'', {}, {'error': 'not_found', 'message': 'No stop'}))
    assert "not registered" in leaf_gantry_check('gantry.local', 's03', 'rg', get=missing)
    down = FakeGet(raises=requests.ConnectionError("refused"))
    assert "could not be reached" in leaf_gantry_check('gantry.local', 's03', 'rg', get=down)
