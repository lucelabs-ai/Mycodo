# -*- coding: utf-8 -*-
import datetime
import json
import logging
import os
import threading
import time

from mycodo.config import MYCODO_DB_PATH
from mycodo.config import PATH_CAMERAS
from mycodo.databases.models import Camera
from mycodo.databases.models import CustomController
from mycodo.databases.models import OutputChannel
from mycodo.databases.utils import session_scope
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.system_pi import assure_path_exists
from mycodo.utils.system_pi import cmd_output
from mycodo.utils.system_pi import set_user_grp
from mycodo.utils.utils import random_alphanumeric

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------
# Fixed USB port -> stable device path mapping for 'leaf_usb' cameras.
#
# Confirmed by plugging one camera into each of the 4 physical USB ports
# in turn and inspecting /dev/v4l/by-path/ -- each port enumerates under
# the same PCIe/USB controller path with only the final ".N" segment
# (1.1 / 1.2 / 1.3 / 1.4) changing per port. "video-index0" is the actual
# capture-capable node for these cameras (video-index1 is metadata-only).
#
# If Mycodo is ever deployed on different hardware (a different board can
# have a different PCIe/USB controller address), re-run the by-path check
# with one camera per port and update this template to match.
# -----------------------------------------------------------------------
LEAF_USB_PORT_DEVICE_TEMPLATE = (
    "/dev/v4l/by-path/platform-fd500000.pcie-pci-0000:01:00.0-"
    "usb-0:1.{port}:1.0-video-index0"
)

# On many single-board computers all USB ports share one upstream USB
# controller, so two 'leaf_usb' cameras capturing at the same instant can
# corrupt each other's frames. This lock is process-wide (shared by every
# 'leaf_usb' Camera captured by this daemon) so at most one such capture
# ever happens at a time, regardless of which port/device it uses.
_LEAF_USB_CAPTURE_LOCK = threading.Lock()


#
# Camera record
#

def camera_record(record_type, unique_id, duration_sec=None, tmp_filename=None):
    """
    Record still image from cameras
    :param record_type:
    :param unique_id:
    :param duration_sec:
    :param tmp_filename:
    :return:
    """
    daemon_control = None

    if db_retrieve_table_daemon(Camera, unique_id=unique_id):
        settings = db_retrieve_table_daemon(Camera, unique_id=unique_id)
    elif db_retrieve_table_daemon(CustomController, unique_id=unique_id):
        settings = db_retrieve_table_daemon(CustomController, unique_id=unique_id)
    else:
        logger.error(f"Camera with ID {unique_id} not found")
        return None, None

    timestamp_date = datetime.datetime.now()
    timestamp = timestamp_date.strftime('%Y-%m-%d_%H-%M-%S')
    assure_path_exists(PATH_CAMERAS)
    camera_path = assure_path_exists(
        os.path.join(PATH_CAMERAS, settings.unique_id))

    if record_type == 'photo':
        if tmp_filename:
            save_path = "/tmp"
        elif settings.path_still:
            save_path = settings.path_still
        else:
            save_path = assure_path_exists(os.path.join(camera_path, 'still'))
        # TODO: next major version, remove cam id (unique_id is already in path)
        filename = f'Still-{settings.id}-{settings.name}-{timestamp}.jpg'.replace(" ", "_")

    elif record_type == 'timelapse':
        if tmp_filename:
            save_path = "/tmp"
        elif settings.path_timelapse:
            save_path = settings.path_timelapse
        else:
            save_path = assure_path_exists(os.path.join(camera_path, 'timelapse'))
        start = datetime.datetime.fromtimestamp(
            settings.timelapse_start_time).strftime("%Y-%m-%d_%H-%M-%S")
        # TODO: next major version, remove cam id (unique_id is already in path)
        filename = f'Timelapse-{settings.id}-{settings.name}-{start}-img-{settings.timelapse_capture_number:05d}.jpg'.replace(" ", "_")

    elif record_type == 'video':
        if tmp_filename:
            save_path = "/tmp"
        elif settings.path_video:
            save_path = settings.path_video
        else:
            save_path = assure_path_exists(os.path.join(camera_path, 'video'))
        filename = f'Video-{settings.name}-{timestamp}.h264'.replace(" ", "_")

    else:
        return None, None

    assure_path_exists(save_path)

    if tmp_filename:
        filename = tmp_filename

    path_file = os.path.join(save_path, filename)

    if tmp_filename and os.path.exists(path_file):
        # This is a throwaway scratch file meant to be freshly overwritten
        # every call. If a previous capture ran as a different user/process
        # than this one, the file's ownership can block a later write with
        # a silent (or hard-to-see) permission error -- removing it first
        # lets whichever process captures next create it fresh with its own
        # ownership instead of fighting the old one.
        try:
            os.remove(path_file)
        except Exception as err:
            logger.warning(f"Could not remove stale temp file {path_file}: {err}")

    # Turn on output, if configured
    output_already_on = False
    output_id = None
    output_channel_id = None
    output_channel = None
    if settings.output_id and ',' in settings.output_id:
        output_id = settings.output_id.split(",")[0]
        output_channel_id = settings.output_id.split(",")[1]
        output_channel = db_retrieve_table_daemon(OutputChannel, unique_id=output_channel_id)

    if output_id and output_channel:
        daemon_control = DaemonControl()
        if daemon_control.output_state(output_id, output_channel=output_channel.channel) == "on":
            output_already_on = True
        else:
            daemon_control.output_on(output_id, output_channel=output_channel.channel)

    # Pause while the output remains on for the specified duration.
    # Used for instance to allow fluorescent lights to fully turn on before
    # capturing an image.
    if settings.output_duration:
        time.sleep(settings.output_duration)

    if settings.library == 'picamera':
        try:
            import picamera

            # Try 5 times to access the pi camera (in case another process is accessing it)
            for _ in range(5):
                try:
                    with picamera.PiCamera() as camera:
                        camera.resolution = (settings.width, settings.height)
                        camera.hflip = settings.hflip
                        camera.vflip = settings.vflip
                        camera.rotation = settings.rotation
                        camera.brightness = int(settings.brightness)
                        camera.contrast = int(settings.contrast)
                        camera.exposure_compensation = int(settings.exposure)
                        camera.saturation = int(settings.saturation)
                        camera.shutter_speed = settings.picamera_shutter_speed
                        camera.sharpness = settings.picamera_sharpness
                        camera.iso = settings.picamera_iso
                        camera.awb_mode = settings.picamera_awb
                        if settings.picamera_awb == 'off':
                            camera.awb_gains = (settings.picamera_awb_gain_red,
                                                settings.picamera_awb_gain_blue)
                        camera.exposure_mode = settings.picamera_exposure_mode
                        camera.meter_mode = settings.picamera_meter_mode
                        camera.image_effect = settings.picamera_image_effect

                        camera.start_preview()
                        time.sleep(2)  # Camera warm-up time

                        if record_type in ['photo', 'timelapse']:
                            camera.capture(path_file, use_video_port=False)
                        elif record_type == 'video':
                            camera.start_recording(path_file, format='h264', quality=20)
                            camera.wait_recording(duration_sec)
                            camera.stop_recording()
                        else:
                            return None, None
                        break
                except picamera.exc.PiCameraMMALError:
                    logger.error("The camera is already open by picamera. Retrying 4 times.")
                time.sleep(1)
        except:
            logger.exception("picamera")

    elif settings.library == 'fswebcam':
        if not os.path.exists("/usr/bin/fswebcam"):
            logger.error("/usr/bin/fswebcam not found")
            return None, None

        try:
            cmd = f"/usr/bin/fswebcam " \
                  f"--device {settings.device} " \
                  f"--resolution {settings.width}x{settings.height} " \
                  f"--set brightness={settings.brightness}% " \
                  f"--no-banner"

            if settings.custom_options:
                cmd += f" {settings.custom_options}"

            if settings.hflip and settings.vflip:
                cmd += " --flip h,v"
            elif settings.hflip:
                cmd += " --flip h"
            elif settings.vflip:
                cmd += " --flip v"

            if settings.rotation:
                cmd += f" --rotate {settings.rotation}"

            cmd += f" --save {path_file}"

            out, err, status = cmd_output(cmd, stdout_pipe=False, user='root')
            logger.debug(
                "Camera debug message: "
                f"cmd: {cmd}; out: {out}; error: {err}; status: {status}")
        except:
            logger.exception("fswebcam")

    elif settings.library == 'raspistill':
        if not os.path.exists("/usr/bin/raspistill"):
            logger.error("/usr/bin/raspistill not found")
            return None, None

        try:
            cmd = f"/usr/bin/raspistill " \
                  f"-w {settings.width} " \
                  f"-h {settings.height} " \
                  f"--brightness {settings.brightness} " \
                  f"-o {path_file}"

            if settings.contrast is not None:
                cmd += f" --contrast {int(settings.contrast)}"
            if settings.saturation is not None:
                cmd += f" --saturation {int(settings.saturation)}"
            if settings.picamera_sharpness is not None:
                cmd += f" --sharpness {int(settings.picamera_sharpness)}"
            if settings.picamera_iso not in [0, None]:
                cmd += f" --ISO {int(settings.picamera_iso)}"
            if settings.picamera_shutter_speed is not None:
                cmd += f" --shutter {int(settings.picamera_shutter_speed)}"
            if settings.picamera_awb not in ["off", None]:
                cmd += f" --awb {settings.picamera_awb}"
            elif (settings.picamera_awb == "off" and
                  settings.picamera_awb_gain_blue is not None and
                  settings.picamera_awb_gain_red is not None):
                cmd += f" --awb {settings.picamera_awb}"
                cmd += f" --awbgains {settings.picamera_awb_gain_red:.1f},{settings.picamera_awb_gain_blue:.1f}"
            if settings.hflip:
                cmd += " --hflip"
            if settings.vflip:
                cmd += " --vflip"
            if settings.rotation:
                cmd += f" --rotation {settings.rotation}"
            if settings.custom_options:
                cmd += f" {settings.custom_options}"

            out, err, status = cmd_output(cmd, stdout_pipe=False, user='root')
            logger.debug(
                "Camera debug message: "
                f"cmd: {cmd}; out: {out}; error: {err}; status: {status}")
        except:
            logger.exception("raspistill")

    elif settings.library == 'libcamera':
        if not os.path.exists("/usr/bin/libcamera-still"):
            logger.error("/usr/bin/libcamera-still found")
            return None, None

        try:
            if settings.output_format:
                # replace extension
                filename = filename.rsplit('.', 1)[0]
                filename = f"{filename}.{settings.output_format.lower()}"
                path_file = os.path.join(save_path, filename)

            cmd = f"/usr/bin/libcamera-still " \
                  f"--width {settings.width} " \
                  f"--height {settings.height} " \
                  f"--brightness {settings.brightness} " \
                  f"-o {path_file}"

            if settings.output_format:
                cmd += f" --encoding {settings.output_format}"
            if not settings.show_preview:
                cmd += " --nopreview"
            if settings.contrast is not None:
                cmd += f" --contrast {int(settings.contrast)}"
            if settings.saturation is not None:
                cmd += f" --saturation {int(settings.saturation)}"
            if settings.picamera_sharpness is not None:
                cmd += f" --sharpness {int(settings.picamera_sharpness)}"
            if settings.picamera_shutter_speed is not None:
                cmd += f" --shutter {int(settings.picamera_shutter_speed)}"
            if settings.gain is not None:
                cmd += f" --gain {settings.gain}"
            if settings.picamera_awb not in ["off", None]:
                cmd += f" --awb {settings.picamera_awb}"
            elif (settings.picamera_awb == "off" and
                  settings.picamera_awb_gain_blue is not None and
                  settings.picamera_awb_gain_red is not None):
                cmd += f" --awb custom"
                cmd += f" --awbgains {settings.picamera_awb_gain_red:.1f},{settings.picamera_awb_gain_blue:.1f}"
            if settings.hflip:
                cmd += " --hflip"
            if settings.vflip:
                cmd += " --vflip"
            if settings.rotation:
                cmd += f" --rotation {settings.rotation}"
            if settings.custom_options:
                cmd += f" {settings.custom_options}"

            out, err, status = cmd_output(cmd, stdout_pipe=False, user='root')
            logger.debug(
                "Camera debug message: "
                f"cmd: {cmd}; out: {out}; error: {err}; status: {status}")
        except:
            logger.exception("libcamera")

    elif settings.library == 'rpicam':
        if not os.path.exists("/usr/bin/rpicam-still"):
            logger.error("/usr/bin/rpicam-still found")
            return None, None

        try:
            if settings.output_format:
                # replace extension
                filename = filename.rsplit('.', 1)[0]
                filename = f"{filename}.{settings.output_format.lower()}"
                path_file = os.path.join(save_path, filename)

            cmd = f"/usr/bin/rpicam-still " \
                  f"--width {settings.width} " \
                  f"--height {settings.height} " \
                  f"--brightness {settings.brightness} " \
                  f"-o {path_file}"

            if settings.output_format:
                cmd += f" --encoding {settings.output_format}"
            if not settings.show_preview:
                cmd += " --nopreview"
            if settings.contrast is not None:
                cmd += f" --contrast {int(settings.contrast)}"
            if settings.saturation is not None:
                cmd += f" --saturation {int(settings.saturation)}"
            if settings.picamera_sharpness is not None:
                cmd += f" --sharpness {int(settings.picamera_sharpness)}"
            if settings.picamera_shutter_speed is not None:
                cmd += f" --shutter {int(settings.picamera_shutter_speed)}"
            if settings.gain is not None:
                cmd += f" --gain {settings.gain}"
            if settings.picamera_awb not in ["off", None]:
                cmd += f" --awb {settings.picamera_awb}"
            elif (settings.picamera_awb == "off" and
                  settings.picamera_awb_gain_blue is not None and
                  settings.picamera_awb_gain_red is not None):
                cmd += f" --awb custom"
                cmd += f" --awbgains {settings.picamera_awb_gain_red:.1f},{settings.picamera_awb_gain_blue:.1f}"
            if settings.hflip:
                cmd += " --hflip"
            if settings.vflip:
                cmd += " --vflip"
            if settings.rotation:
                cmd += f" --rotation {settings.rotation}"
            if settings.custom_options:
                cmd += f" {settings.custom_options}"

            out, err, status = cmd_output(cmd, stdout_pipe=False, user='root')
            logger.debug(
                "Camera debug message: "
                f"cmd: {cmd}; out: {out}; error: {err}; status: {status}")
        except:
            logger.exception("rpicam")

    elif settings.library == 'opencv':
        try:
            import cv2
            import imutils

            cap = cv2.VideoCapture(settings.opencv_device)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, settings.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, settings.height)
            cap.set(cv2.CAP_PROP_EXPOSURE, settings.exposure)
            cap.set(cv2.CAP_PROP_GAIN, settings.gain)
            cap.set(cv2.CAP_PROP_BRIGHTNESS, settings.brightness)
            cap.set(cv2.CAP_PROP_CONTRAST, settings.contrast)
            cap.set(cv2.CAP_PROP_HUE, settings.hue)
            cap.set(cv2.CAP_PROP_SATURATION, settings.saturation)

            # Check if image can be read
            status, _ = cap.read()
            if not status:
                logger.error(
                    f"Cannot detect USB camera with device '{settings.custom_options}'")
                return None, None

            # Discard a few frames to allow camera to adjust to settings
            for _ in range(2):
                cap.read()

            if record_type in ['photo', 'timelapse']:
                edited = False
                status, img_orig = cap.read()
                cap.release()

                if not status:
                    logger.error("Could not acquire image")
                    return None, None

                img_edited = img_orig.copy()

                if any((settings.hflip, settings.vflip, settings.rotation)):
                    edited = True

                if settings.hflip and settings.vflip:
                    img_edited = cv2.flip(img_orig, -1)
                elif settings.hflip:
                    img_edited = cv2.flip(img_orig, 1)
                elif settings.vflip:
                    img_edited = cv2.flip(img_orig, 0)

                if settings.rotation:
                    img_edited = imutils.rotate_bound(img_orig, settings.rotation)

                if edited:
                    write_success = cv2.imwrite(path_file, img_edited)
                else:
                    write_success = cv2.imwrite(path_file, img_orig)

                if not write_success:
                    logger.error(f"Could not write image to {path_file}")
                    return None, None

            elif record_type == 'video':
                # TODO: opencv video recording is currently not working. No idea why. Try to fix later.
                try:
                    cap = cv2.VideoCapture(settings.opencv_device)
                    fourcc = cv2.CV_FOURCC('X', 'V', 'I', 'D')
                    resolution = (settings.width, settings.height)
                    out = cv2.VideoWriter(path_file, fourcc, 20.0, resolution)

                    time_end = time.time() + duration_sec
                    while cap.isOpened() and time.time() < time_end:
                        ret, frame = cap.read()
                        if ret:
                            # write the frame
                            out.write(frame)
                            if cv2.waitKey(1) & 0xFF == ord('q'):
                                break
                        else:
                            break
                    cap.release()
                    out.release()
                    cv2.destroyAllWindows()
                except Exception as err:
                    logger.exception(
                        f"Exception raised while recording video: {err}")
            else:
                return None, None
        except:
            logger.exception("opencv")

    elif settings.library == 'leaf_usb':
        if record_type not in ['photo', 'timelapse']:
            logger.error("The leaf_usb library only supports still images (photo/timelapse), not video.")
            return None, None

        # Serialized process-wide: see _LEAF_USB_CAPTURE_LOCK comment above.
        with _LEAF_USB_CAPTURE_LOCK:
            try:
                import cv2

                port = str(settings.device).strip()
                if port not in ('1', '2', '3', '4'):
                    logger.error(
                        f"leaf_usb 'device' must be a USB port number (1-4), got: {settings.device!r}")
                    return None, None
                device_path = LEAF_USB_PORT_DEVICE_TEMPLATE.format(port=port)

                # Open/close the device fresh for every single capture
                # (rather than holding it open continuously) so a camera
                # only holds USB bandwidth for the brief moment it's
                # actually grabbing a frame.
                cap = cv2.VideoCapture(device_path, cv2.CAP_V4L2)
                try:
                    if not cap.isOpened():
                        logger.error(f"Could not open camera on USB port {port} ({device_path})")
                        return None, None

                    # Request MJPG (compressed) rather than the raw default
                    # to cut USB bandwidth. Must be set before width/height
                    # to take effect on most UVC cameras.
                    cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, settings.width)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, settings.height)

                    # A value of -1 (the default) on brightness/contrast/
                    # saturation/gain means "leave this V4L2 control at the
                    # camera's own default" rather than force a value.
                    if settings.brightness is not None and settings.brightness >= 0:
                        cap.set(cv2.CAP_PROP_BRIGHTNESS, settings.brightness)
                    if settings.contrast is not None and settings.contrast >= 0:
                        cap.set(cv2.CAP_PROP_CONTRAST, settings.contrast)
                    if settings.saturation is not None and settings.saturation >= 0:
                        cap.set(cv2.CAP_PROP_SATURATION, settings.saturation)
                    if settings.gain is not None and settings.gain >= 0:
                        cap.set(cv2.CAP_PROP_GAIN, settings.gain)

                    # Exposure left unset (None) means auto-exposure stays on.
                    if settings.exposure is not None:
                        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 1)  # manual, most V4L2 UVC drivers
                        cap.set(cv2.CAP_PROP_EXPOSURE, settings.exposure)
                    else:
                        cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 3)  # auto, most V4L2 UVC drivers

                    # Discard a couple of frames while auto-exposure/gain
                    # settle and the format switch takes effect.
                    for _ in range(2):
                        cap.read()
                    status, img_orig = cap.read()
                finally:
                    cap.release()

                if not status or img_orig is None:
                    logger.error(f"Could not acquire image from USB port {port} ({device_path})")
                    return None, None

                img_edited = img_orig
                if settings.hflip and settings.vflip:
                    img_edited = cv2.flip(img_edited, -1)
                elif settings.hflip:
                    img_edited = cv2.flip(img_edited, 1)
                elif settings.vflip:
                    img_edited = cv2.flip(img_edited, 0)

                rotation = int(settings.rotation or 0)
                if rotation == 90:
                    img_edited = cv2.rotate(img_edited, cv2.ROTATE_90_CLOCKWISE)
                elif rotation == 180:
                    img_edited = cv2.rotate(img_edited, cv2.ROTATE_180)
                elif rotation == 270:
                    img_edited = cv2.rotate(img_edited, cv2.ROTATE_90_COUNTERCLOCKWISE)
                elif rotation != 0:
                    logger.warning(
                        f"leaf_usb only supports rotation of 0/90/180/270 degrees, ignoring {rotation}")

                write_success = cv2.imwrite(path_file, img_edited)
                if not write_success:
                    logger.error(f"Could not write image to {path_file}")
                    return None, None
            except:
                logger.exception("leaf_usb")

    elif settings.library == 'http_address':
        try:
            import cv2
            import imutils
            from urllib.error import HTTPError
            from urllib.parse import urlparse
            from urllib.request import urlretrieve

            if record_type in ['photo', 'timelapse']:
                path_tmp = f"/tmp/tmpimg_{random_alphanumeric(8)}.jpg"

                try:
                    os.remove(path_tmp)
                except FileNotFoundError:
                    pass

                try:
                    urlretrieve(settings.url_still, path_tmp)
                except HTTPError as err:
                    logger.error(err)
                except Exception as err:
                    logger.exception(err)

                if not os.path.isfile(path_tmp):
                    logger.error("Could not acquire image.")
                else:
                    try:
                        img_orig = cv2.imread(path_tmp)

                        if img_orig is not None and img_orig.shape is not None:
                            if any((settings.hflip, settings.vflip, settings.rotation)):
                                if settings.hflip and settings.vflip:
                                    img_edited = cv2.flip(img_orig, -1)
                                elif settings.hflip:
                                    img_edited = cv2.flip(img_orig, 1)
                                elif settings.vflip:
                                    img_edited = cv2.flip(img_orig, 0)

                                if settings.rotation:
                                    img_edited = imutils.rotate_bound(img_orig, settings.rotation)

                                cv2.imwrite(path_file, img_edited)
                            else:
                                cv2.imwrite(path_file, img_orig)
                        else:
                            os.rename(path_tmp, path_file)
                    except Exception as err:
                        logger.error(f"Could not convert, rotate, or invert image: {err}")
                        try:
                            os.rename(path_tmp, path_file)
                        except FileNotFoundError:
                            logger.error("Can't move image. Camera image not found")

            elif record_type == 'video':
                pass  # No video (yet)
        except:
            logger.exception("http_address")

    elif settings.library == 'http_address_requests':
        try:
            import cv2
            import imutils
            import requests

            try:
                headers = json.loads(settings.json_headers)
            except:
                headers = {}

            if record_type in ['photo', 'timelapse']:
                success = False
                path_tmp = f"/tmp/tmpimg_{random_alphanumeric(8)}.jpg"

                try:
                    os.remove(path_tmp)
                except FileNotFoundError:
                    pass

                try:
                    r = requests.get(settings.url_still, headers=headers, verify=False)
                    if r.status_code == 200:
                        open(path_tmp, 'wb').write(r.content)
                        success = True
                    else:
                        logger.error(f"Could not download image. Status code: {r.status_code}, content: {r.content}")
                except requests.HTTPError as err:
                    logger.error(f"HTTPError: {err}")
                except Exception as err:
                    logger.exception(err)

                if success:
                    img_orig = cv2.imread(path_tmp)

                    if not os.path.isfile(path_tmp):
                        logger.error("Could not acquire image.")
                    else:
                        try:
                            if img_orig is not None and img_orig.shape is not None:
                                if any((settings.hflip, settings.vflip, settings.rotation)):
                                    if settings.hflip and settings.vflip:
                                        img_edited = cv2.flip(img_orig, -1)
                                    elif settings.hflip:
                                        img_edited = cv2.flip(img_orig, 1)
                                    elif settings.vflip:
                                        img_edited = cv2.flip(img_orig, 0)

                                    if settings.rotation:
                                        img_edited = imutils.rotate_bound(img_orig, settings.rotation)

                                    cv2.imwrite(path_file, img_edited)
                                else:
                                    cv2.imwrite(path_file, img_orig)
                            else:
                                os.rename(path_tmp, path_file)
                        except Exception as err:
                            logger.error(f"Could not convert, rotate, or invert image: {err}")
                            try:
                                os.rename(path_tmp, path_file)
                            except FileNotFoundError:
                                logger.error("Can't move image. Camera image not found")
            elif record_type == 'video':
                pass  # No video (yet)
        except:
            logger.exception("http_address_requests")

    try:
        set_user_grp(path_file, 'mycodo', 'mycodo')
    except Exception as err:
        logger.exception(
            f"Exception raised in 'camera_record' when setting user grp: {err}")

    # Turn off output, if configured
    if output_id and output_channel and daemon_control and not output_already_on:
        daemon_control.output_off(output_id, output_channel=output_channel.channel)

    if record_type in ['photo', 'timelapse'] and not tmp_filename:
        # Store the filename and timestamp in the database for photos and timestamps
        with session_scope(MYCODO_DB_PATH) as new_session:
            mod_camera = new_session.query(Camera).filter(Camera.unique_id == unique_id).first()
            if record_type == 'photo':
                mod_camera.still_last_file = filename
                mod_camera.still_last_ts = timestamp_date.timestamp()
            elif record_type == 'timelapse':
                mod_camera.timelapse_last_file = filename
                mod_camera.timelapse_last_ts = timestamp_date.timestamp()
            new_session.commit()

    if not os.path.exists(path_file):
        logger.error("No image was created. Check your settings and hardware for any issues.")
    else:
        try:
            set_user_grp(path_file, 'mycodo', 'mycodo')
            return save_path, filename
        except Exception as err:
            logger.exception(
                f"Exception raised in 'camera_record' when setting user grp: {err}")

    return None, None


def count_cameras_opencv():
    """Returns how many cameras are detected with opencv (cv2)"""
    import cv2
    camera_ids = []
    max_tested = 10
    for i in range(max_tested):
        temp_camera = cv2.VideoCapture(i)
        # Many USB/UVC cameras expose more than one /dev/videoN node per
        # physical device (e.g. a separate metadata-only node) -- those
        # nodes often still report isOpened() but never deliver a real
        # frame, so also require a successful read() before counting one
        # as an actual usable camera.
        if temp_camera.isOpened():
            status, frame = temp_camera.read()
            if status and frame is not None:
                camera_ids.append(i)
        temp_camera.release()
    return camera_ids
