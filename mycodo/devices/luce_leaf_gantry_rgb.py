# coding=utf-8
"""leaf_gantry_rgb: a Mycodo camera that relays one stop of a rack gantry.

The gantry (lucelabs-ai/leaf_gantry) runs on its own Raspberry Pi: it moves its
camera along the shelf in rounds and keeps the newest photo of every registered
stop. This camera library takes no photo itself. On every capture it asks the
gantry for one stop's newest photo,

    GET http://<gantry address>/api/v1/stops/<stop id>/latest
    Authorization: Bearer <token>

refuses it if it is older than the camera's maximum photo age, and writes the
gantry's JPEG bytes to Mycodo's file unchanged: native resolution, no decoding
or re-encoding, so nothing is lost on the way to the hub. The gantry is never
asked to move or to take a photo.

Every failure raises LeafGantryError with a message that names the cause
("gantry unreachable", "stop s03 is not registered on the gantry", "photo 17
min old"), which camera.py logs; no file is written, so the bridge's request
fails and it reports the camera's fault.

Settings, all on the Camera page (camera_options/leaf_gantry_rgb.html):
  url_still       the gantry's address, e.g. 192.168.1.50:8080 or gantry-1.local
  device          the stop id, e.g. s03; empty = taken from the camera's name
                  (gantry-s03 -> s03)
  custom_options  JSON {"token": "...", "max_age_min": 15}
"""
import datetime
import json
import os
import re
import tempfile

LEAF_GANTRY_DEFAULT_PORT = 8080
LEAF_GANTRY_DEFAULT_MAX_AGE_MIN = 15.0
LEAF_GANTRY_MAX_AGE_LIMIT_MIN = 1440.0
# (connect, read) seconds. The bridge gives a capture 15 s in all; a stop's
# newest photo is a file the gantry already has, so it normally answers in
# milliseconds.
LEAF_GANTRY_TIMEOUT_SEC = (4.0, 8.0)
# Checking the stop when the camera is saved: quick, so the page never hangs.
LEAF_GANTRY_CHECK_TIMEOUT_SEC = (2.0, 3.0)
LEAF_GANTRY_NAME_PREFIX = 'gantry-'

_STOP_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')
_HOST_RE = re.compile(r'^[A-Za-z0-9.-]+$|^\[[0-9A-Fa-f:]+\]$')


class LeafGantryError(Exception):
    """A capture or a setting failed; the message says why in plain words."""


# -- settings ------------------------------------------------------------------

def leaf_gantry_base_url(address):
    """'http://host:port' from what was typed in the Gantry address field.

    Accepts host, host:port, http(s)://host[:port] with or without a trailing
    slash. The port defaults to 8080.
    """
    text = (address or '').strip().rstrip('/')
    if not text:
        raise LeafGantryError("No gantry address set (for example 192.168.1.50:8080)")
    scheme = 'http'
    for prefix in ('http://', 'https://'):
        if text.lower().startswith(prefix):
            scheme = prefix[:-3]
            text = text[len(prefix):]
            break
    if '/' in text:
        raise LeafGantryError(
            f"Gantry address '{address}' should be only the host and port, "
            f"for example 192.168.1.50:8080")
    host, port = text, LEAF_GANTRY_DEFAULT_PORT
    if text.startswith('['):                       # [ipv6]:port
        end = text.find(']')
        host, rest = text[:end + 1], text[end + 1:]
        if rest:
            port = rest.lstrip(':')
    elif text.count(':') == 1:
        host, port = text.split(':')
    if not host or not _HOST_RE.match(host):
        raise LeafGantryError(f"Gantry address '{address}' is not a host name or IP address")
    try:
        port = int(port)
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise LeafGantryError(f"Gantry address '{address}' has an invalid port")
    return f"{scheme}://{host}:{port}"


def leaf_gantry_stop_id(stop, camera_name=''):
    """The stop id: the Stop ID field, or else taken from the camera's name."""
    stop = (stop or '').strip()
    if not stop:
        name = (camera_name or '').strip()
        if name.lower().startswith(LEAF_GANTRY_NAME_PREFIX):
            stop = name[len(LEAF_GANTRY_NAME_PREFIX):]
        if not stop:
            raise LeafGantryError(
                "No stop ID: fill in Stop ID, or name the camera gantry-<stop id> (for example gantry-s03)")
    if not _STOP_ID_RE.match(stop):
        raise LeafGantryError(
            f"Stop ID '{stop}' is not valid: 1 to 32 lowercase letters, digits, _ or -, "
            f"starting with a letter or digit, as on the gantry's Stops and rounds tab")
    return stop


def leaf_gantry_photo_url(address, stop_id):
    return f"{leaf_gantry_base_url(address)}/api/v1/stops/{stop_id}/latest"


def leaf_gantry_settings(custom_options):
    """(token, max_age_min) from the camera's custom_options JSON."""
    try:
        options = json.loads(custom_options or '{}')
        if not isinstance(options, dict):
            options = {}
    except (TypeError, ValueError):
        options = {}
    token = str(options.get('token') or '')
    try:
        max_age = float(options.get('max_age_min', LEAF_GANTRY_DEFAULT_MAX_AGE_MIN))
    except (TypeError, ValueError):
        max_age = LEAF_GANTRY_DEFAULT_MAX_AGE_MIN
    if not 0 < max_age <= LEAF_GANTRY_MAX_AGE_LIMIT_MIN:
        max_age = LEAF_GANTRY_DEFAULT_MAX_AGE_MIN
    return token, max_age


def leaf_gantry_settings_json(token, max_age_min):
    return json.dumps({'token': token or '', 'max_age_min': float(max_age_min)})


def leaf_gantry_settings_from_form(form, current_custom_options=None):
    """(custom_options JSON, [errors]) from the Camera page's fields.

    `form` maps field names to strings: `leaf_gantry_token` (empty keeps the
    saved token) and `leaf_gantry_max_age`.
    """
    errors = []
    saved_token, saved_age = leaf_gantry_settings(current_custom_options)
    token = (form.get('leaf_gantry_token') or '').strip()
    if token.lower().startswith('bearer '):
        token = token[7:].strip()
    if not token:
        token = saved_token
    elif any(c.isspace() for c in token):
        errors.append("The access token must not contain spaces")
    raw_age = (form.get('leaf_gantry_max_age') or '').strip()
    max_age = saved_age
    if raw_age:
        try:
            max_age = float(raw_age)
            if not 0 < max_age <= LEAF_GANTRY_MAX_AGE_LIMIT_MIN:
                raise ValueError
        except ValueError:
            errors.append(
                f"Maximum photo age must be a number of minutes above 0 and up to "
                f"{LEAF_GANTRY_MAX_AGE_LIMIT_MIN:g}")
            max_age = saved_age
    return leaf_gantry_settings_json(token, max_age), errors


def leaf_gantry_page_info(device, custom_options, camera_name):
    """What the Camera page shows for one leaf_gantry_rgb camera."""
    token, max_age = leaf_gantry_settings(custom_options)
    try:
        default_stop = leaf_gantry_stop_id('', camera_name)
    except LeafGantryError:
        default_stop = ''
    return {'stop': (device or '').strip(), 'default_stop': default_stop,
            'max_age': max_age, 'has_token': bool(token)}


# -- talking to the gantry -------------------------------------------------------------

def _headers(token):
    headers = {'Accept': 'image/jpeg, application/json'}
    if token:
        headers['Authorization'] = f"Bearer {token}"
    return headers


def _gantry_message(response):
    try:
        body = response.json()
        return body.get('error', ''), body.get('message', '')
    except ValueError:
        return '', (response.text or '')[:200]


def _raise_for_answer(response, base, stop_id):
    code = response.status_code
    error, message = _gantry_message(response)
    if code == 401:
        raise LeafGantryError(
            f"The gantry at {base} rejected the access token: create a read token on the "
            f"gantry's Integrations tab and paste it in Access token")
    if code == 403:
        raise LeafGantryError(f"The gantry at {base} refused the request: {message or 'forbidden'}")
    if code == 404 and error == 'no_photo':
        raise LeafGantryError(
            f"The gantry has no photo yet for stop {stop_id}: is it in a round, and has a round "
            f"run since it was added?")
    if code == 404:
        raise LeafGantryError(
            f"Stop {stop_id} is not registered on the gantry at {base}: add it on the "
            f"gantry's Stops and rounds tab")
    raise LeafGantryError(
        f"The gantry at {base} answered HTTP {code}{': ' + message if message else ''}")


def _photo_age_min(headers, now):
    """Minutes since the photo was taken, by the gantry's own clock if it says.

    X-Photo-Age-S is measured on the gantry, so a clock that differs between
    the two Pis does not matter; X-Captured-At (ISO 8601 with offset) is the
    fallback.
    """
    age_s = headers.get('X-Photo-Age-S')
    if age_s:
        try:
            return float(age_s) / 60.0
        except ValueError:
            pass
    captured = headers.get('X-Captured-At')
    if captured:
        try:
            ts = datetime.datetime.fromisoformat(captured.replace('Z', '+00:00'))
            if ts.tzinfo is None:
                ts = ts.astimezone()
            now = now or datetime.datetime.now(datetime.timezone.utc)
            return (now - ts).total_seconds() / 60.0
        except ValueError:
            pass
    raise LeafGantryError("The gantry did not say when the photo was taken, so its age cannot be checked")


def leaf_gantry_fetch(address, stop_id, token, max_age_min, get=None, now=None):
    """(jpeg bytes, info) for the stop's newest photo, or LeafGantryError.

    info: {'captured_at', 'capture_id', 'age_min', 'url'}. `get` and `now` are
    for tests (requests.get and the current UTC time by default).
    """
    base = leaf_gantry_base_url(address)
    url = f"{base}/api/v1/stops/{stop_id}/latest"
    if get is None:
        import requests
        get = requests.get
    try:
        import requests
        timeout_errors = (requests.Timeout,)
        connection_errors = (requests.ConnectionError,)
        request_errors = (requests.RequestException,)
    except ImportError:  # pragma: no cover - requests ships with Mycodo
        timeout_errors = connection_errors = request_errors = ()
    try:
        response = get(url, headers=_headers(token), timeout=LEAF_GANTRY_TIMEOUT_SEC)
    except timeout_errors:
        raise LeafGantryError(
            f"The gantry at {base} did not answer within {sum(LEAF_GANTRY_TIMEOUT_SEC):g} s")
    except connection_errors as err:
        raise LeafGantryError(f"Gantry unreachable at {base} ({type(err).__name__})")
    except request_errors as err:
        raise LeafGantryError(f"Request to the gantry at {base} failed: {err}")
    if response.status_code != 200:
        _raise_for_answer(response, base, stop_id)
    data = response.content or b''
    if not data.startswith(b'\xff\xd8'):
        raise LeafGantryError(
            f"The gantry at {base} did not send a JPEG for stop {stop_id} "
            f"({response.headers.get('Content-Type', 'no content type')})")
    age = _photo_age_min(response.headers, now)
    if age > max_age_min:
        shown = f"{age:.1f}" if age < 10 else f"{age:.0f}"
        raise LeafGantryError(
            f"The newest photo of stop {stop_id} is {shown} min old (limit {max_age_min:g} min): "
            f"are rounds running on the gantry?")
    return data, {'captured_at': response.headers.get('X-Captured-At'),
                  'capture_id': response.headers.get('X-Capture-Id'),
                  'age_min': round(age, 2), 'url': url}


def leaf_gantry_save(data, path_file):
    """Write the bytes exactly as received, atomically (temp file, rename)."""
    folder = os.path.dirname(path_file) or '.'
    fd, tmp = tempfile.mkstemp(prefix='.leaf_gantry_', suffix='.jpg', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(tmp, 0o644)  # as an image written by any other library (mkstemp makes 0600)
        os.replace(tmp, path_file)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def leaf_gantry_check(address, stop_id, token, get=None):
    """A quick look when the camera is saved: '' if the stop is there, else why not.

    Asks GET /api/v1/stops/<id> (the stop's record, no photo); never fails the save.
    """
    try:
        base = leaf_gantry_base_url(address)
    except LeafGantryError as err:
        return str(err)
    if get is None:
        import requests
        get = requests.get
    try:
        response = get(f"{base}/api/v1/stops/{stop_id}", headers=_headers(token),
                       timeout=LEAF_GANTRY_CHECK_TIMEOUT_SEC)
    except Exception as err:  # noqa: BLE001 - only a warning on the page
        return f"Saved, but the gantry at {base} could not be reached ({type(err).__name__})"
    if response.status_code == 200:
        return ''
    try:
        _raise_for_answer(response, base, stop_id)
    except LeafGantryError as err:
        return f"Saved, but: {err}"
    return ''
