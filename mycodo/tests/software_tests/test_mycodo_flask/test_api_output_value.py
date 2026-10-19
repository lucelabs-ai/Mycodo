# coding=utf-8
"""tests for POST /api/outputs/<unique_id>: the "value" field and the fields it must not disturb."""
import base64
import json

import mock

OUTPUT_ID = 'abcdef12-3456-7890-abcd-ef1234567890'
HEADERS = {
    'Accept': 'application/vnd.mycodo.v1+json',
    'X-API-KEY': base64.b64encode(b'secret_admin_api_key'),
    'Content-Type': 'application/json',
}


def post_output(testapp, daemon, payload, expect_errors=False):
    """POST a payload to the output endpoint with the daemon replaced by a mock."""
    with mock.patch('mycodo.mycodo_flask.api.output.DaemonControl', return_value=daemon):
        return testapp.post(
            '/api/outputs/{id}'.format(id=OUTPUT_ID),
            json.dumps(payload),
            HEADERS,
            expect_errors=expect_errors)


def make_daemon():
    daemon = mock.MagicMock()
    daemon.output_on.return_value = (0, 'ok')
    daemon.output_on_off.return_value = (0, 'ok')
    return daemon


def test_value_is_sent_as_a_value_output(testapp):
    """{"value": 5.5} reaches the daemon as output_type 'value' with that amount."""
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 1, 'value': 5.5})
    assert response.status_code == 200
    daemon.output_on.assert_called_once_with(
        OUTPUT_ID, output_channel=1, output_type='value', amount=5.5)
    daemon.output_on_off.assert_not_called()


def test_negative_value_is_not_clamped(testapp):
    """A stepper takes signed steps: a negative value reaches the daemon unchanged."""
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 0, 'value': -200})
    assert response.status_code == 200
    daemon.output_on.assert_called_once_with(
        OUTPUT_ID, output_channel=0, output_type='value', amount=-200.0)


def test_zero_value_is_sent(testapp):
    """0 is a real value (a DAC at 0 V), not 'no value'."""
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 0, 'value': 0})
    assert response.status_code == 200
    daemon.output_on.assert_called_once_with(
        OUTPUT_ID, output_channel=0, output_type='value', amount=0.0)


def test_non_numeric_value_is_refused(testapp):
    """A value that is not a number is a 422 and reaches nothing."""
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 0, 'value': 'abc'}, expect_errors=True)
    assert response.status_code == 422
    daemon.output_on.assert_not_called()
    daemon.output_on_off.assert_not_called()


def test_channel_alone_is_still_insufficient(testapp):
    """No actionable field still returns 460 'Insufficient payload'."""
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 0}, expect_errors=True)
    assert response.status_code == 460
    daemon.output_on.assert_not_called()
    daemon.output_on_off.assert_not_called()


def test_daemon_error_is_returned(testapp):
    """A daemon-side failure on a value output comes back as the existing 460 'Fail'."""
    daemon = make_daemon()
    daemon.output_on.return_value = (1, 'Output not set up')
    response = post_output(testapp, daemon, {'channel': 0, 'value': 3}, expect_errors=True)
    assert response.status_code == 460
    assert 'Fail' in response.text


# The fields that existed before "value" must keep their meaning.
def test_duty_cycle_still_pwm(testapp):
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 0, 'duty_cycle': 50})
    assert response.status_code == 200
    daemon.output_on.assert_called_once_with(
        OUTPUT_ID, output_channel=0, output_type='pwm', amount=50.0)


def test_volume_still_vol(testapp):
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 0, 'volume': 35})
    assert response.status_code == 200
    daemon.output_on.assert_called_once_with(
        OUTPUT_ID, output_channel=0, output_type='vol', amount=35.0)


def test_state_and_duration_still_seconds(testapp):
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 0, 'state': True, 'duration': 10})
    assert response.status_code == 200
    daemon.output_on_off.assert_called_once_with(
        OUTPUT_ID, True, output_channel=0, output_type='sec', amount=10.0)
    daemon.output_on.assert_not_called()


def test_state_alone_still_on_off(testapp):
    daemon = make_daemon()
    response = post_output(testapp, daemon, {'channel': 0, 'state': False})
    assert response.status_code == 200
    daemon.output_on_off.assert_called_once_with(
        OUTPUT_ID, False, output_channel=0)


def test_duty_cycle_takes_precedence_over_value(testapp):
    """Branch order is unchanged for existing fields: duty_cycle is checked before value."""
    daemon = make_daemon()
    post_output(testapp, daemon, {'channel': 0, 'duty_cycle': 25, 'value': 9})
    daemon.output_on.assert_called_once_with(
        OUTPUT_ID, output_channel=0, output_type='pwm', amount=25.0)


def test_value_takes_precedence_over_volume(testapp):
    """The one behaviour that differs from before: a payload with both now takes 'value'."""
    daemon = make_daemon()
    post_output(testapp, daemon, {'channel': 0, 'volume': 3, 'value': 9})
    daemon.output_on.assert_called_once_with(
        OUTPUT_ID, output_channel=0, output_type='value', amount=9.0)
