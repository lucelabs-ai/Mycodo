# coding=utf-8
"""The leaf_gantry_rgb camera through Mycodo itself: added and saved on the Camera
page, and a capture through camera_record() that relays the gantry's bytes unchanged.
No gantry or network: requests.get is replaced.
"""
import json
import os
import types

import mock

from mycodo.databases.models import Camera
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_settings
from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_settings_json

JPEG = b'\xff\xd8\xff\xe0' + os.urandom(4096) + b'\xff\xd9'


class FakeResponse:
    def __init__(self, status=200, content=b'', headers=None, body=None):
        self.status_code = status
        self.content = content
        self.headers = headers or {}
        self._body = body
        self.text = json.dumps(body) if body is not None else ''

    def json(self):
        if self._body is None:
            raise ValueError
        return self._body


def add_and_save(app, fields, check_answer=''):
    """Add a leaf_gantry_rgb camera, then save the Camera page's fields, as camera_mod does."""
    from mycodo.mycodo_flask.forms import forms_camera
    from mycodo.mycodo_flask.utils import utils_camera
    app.config['WTF_CSRF_ENABLED'] = False
    with app.test_request_context('/camera', method='POST',
                                  data={'name': 'gantry-s03', 'library': 'leaf_gantry_rgb', 'camera_add': 'Add'}):
        utils_camera.camera_add(forms_camera.Camera())
    camera = Camera.query.filter(Camera.name == 'gantry-s03').first()
    data = {'camera_id': camera.unique_id, 'name': 'gantry-s03', 'rotation': '0', 'camera_mod': 'Save'}
    data.update(fields)
    with app.test_request_context('/camera_submit', method='POST', data=data), \
            mock.patch('mycodo.mycodo_flask.utils.utils_camera.leaf_gantry_check',
                       return_value=check_answer) as check:
        messages = utils_camera.camera_mod(forms_camera.Camera())
    return messages, check, Camera.query.filter(Camera.name == 'gantry-s03').first()


def test_add_and_save_a_gantry_camera(app, db):
    messages, check, camera = add_and_save(app, {
        'leaf_gantry_address': '192.168.1.50', 'leaf_gantry_stop': '',
        'leaf_gantry_token': 'rg_secret_token_123', 'leaf_gantry_max_age': '15'})
    assert messages['error'] == [], messages
    assert camera.library == 'leaf_gantry_rgb'
    check.assert_called_once_with('192.168.1.50', 's03', 'rg_secret_token_123')
    assert camera.url_still == '192.168.1.50' and camera.device == ''
    assert leaf_gantry_settings(camera.custom_options) == ('rg_secret_token_123', 15.0)


def test_empty_token_keeps_the_saved_one_and_a_check_problem_is_a_warning(app, db):
    add_and_save(app, {'leaf_gantry_address': 'gantry.local', 'leaf_gantry_token': 'rg_first'})
    from mycodo.mycodo_flask.forms import forms_camera
    from mycodo.mycodo_flask.utils import utils_camera
    camera = Camera.query.filter(Camera.name == 'gantry-s03').first()
    data = {'camera_id': camera.unique_id, 'name': 'gantry-s03', 'rotation': '0', 'camera_mod': 'Save',
            'leaf_gantry_address': 'gantry.local', 'leaf_gantry_stop': 's07',
            'leaf_gantry_token': '', 'leaf_gantry_max_age': '20'}
    with app.test_request_context('/camera_submit', method='POST', data=data), \
            mock.patch('mycodo.mycodo_flask.utils.utils_camera.leaf_gantry_check',
                       return_value='Saved, but: Stop s07 is not registered on the gantry'):
        messages = utils_camera.camera_mod(forms_camera.Camera())
    assert messages['error'] == [] and 'not registered' in messages['warning'][0]
    camera = Camera.query.filter(Camera.name == 'gantry-s03').first()
    assert camera.device == 's07'
    assert leaf_gantry_settings(camera.custom_options) == ('rg_first', 20.0)


def test_bad_address_is_refused(app, db):
    messages, check, _ = add_and_save(app, {'leaf_gantry_address': 'http://gantry/api'})
    assert messages['error'] and 'only the host and port' in messages['error'][0]
    check.assert_not_called()


def test_options_page_renders_without_the_token(app, db):
    from flask import render_template_string
    from mycodo.devices.luce_leaf_gantry_rgb import leaf_gantry_page_info
    cam = types.SimpleNamespace(unique_id='u1', url_still='gantry.local', device='',
                                custom_options=leaf_gantry_settings_json('rg_secret_token_123', 15))
    with app.test_request_context():
        app.jinja_env.get_template('pages/camera.html')   # the page itself still compiles
        html = render_template_string(
            '{% include "pages/camera_options/leaf_gantry_rgb.html" %}', each_camera=cam,
            leaf_gantry_info={'u1': leaf_gantry_page_info('', cam.custom_options, 'gantry-s03')},
            leaf_gantry_default_port=8080, leaf_gantry_max_age_limit=1440)
    assert 'rg_secret_token_123' not in html
    assert 'Saved: leave empty to keep it' in html
    assert '/api/v1/stops/s03/latest' in html
    assert 'name="rotation" value="0"' in html


def settings(**kw):
    s = types.SimpleNamespace(
        id=1, unique_id='cam-gantry-s03', name='gantry-s03', library='leaf_gantry_rgb',
        device='', url_still='gantry.local:8080',
        custom_options=leaf_gantry_settings_json('rg_tok', 15), output_id=None, output_duration=0,
        path_still='', path_timelapse='', path_video='', cmd_pre_camera='', cmd_post_camera='')
    s.__dict__.update(kw)
    return s


def capture(tmp_path, response, **kw):
    from mycodo.devices import camera as camera_module
    calls = []

    def fake_get(url, headers=None, timeout=None):
        calls.append((url, headers))
        return response

    with mock.patch.object(camera_module, 'db_retrieve_table_daemon', return_value=settings(**kw)), \
            mock.patch.object(camera_module, 'assure_path_exists', side_effect=lambda p: p), \
            mock.patch.object(camera_module, 'set_user_grp'), \
            mock.patch.object(camera_module, 'PATH_CAMERAS', str(tmp_path)), \
            mock.patch('requests.get', side_effect=fake_get):
        name = f'test_gantry_{os.getpid()}.jpg'
        result = camera_module.camera_record('photo', 'cam-gantry-s03', tmp_filename=name)
    return result, calls, os.path.join('/tmp', name)


def test_capture_relays_the_bytes_unchanged(tmp_path):
    ok = FakeResponse(200, JPEG, {'Content-Type': 'image/jpeg', 'X-Photo-Age-S': '240.0',
                                  'X-Captured-At': '2026-11-19T14:56:00.000-06:00'})
    (path, filename), calls, file = capture(tmp_path, ok)
    try:
        assert os.path.join(path, filename) == file
        with open(file, 'rb') as f:
            assert f.read() == JPEG
        url, headers = calls[0]
        assert url == 'http://gantry.local:8080/api/v1/stops/s03/latest'
        assert headers['Authorization'] == 'Bearer rg_tok'
    finally:
        if os.path.exists(file):
            os.remove(file)


def test_stale_or_missing_photo_gives_no_file(tmp_path):
    stale = FakeResponse(200, JPEG, {'Content-Type': 'image/jpeg', 'X-Photo-Age-S': str(16 * 60)})
    result, _, file = capture(tmp_path, stale)
    assert result == (None, None) and not os.path.exists(file)
    missing = FakeResponse(404, b'', {}, {'error': 'not_found', 'message': "No stop 's03'"})
    result, _, file = capture(tmp_path, missing)
    assert result == (None, None) and not os.path.exists(file)
