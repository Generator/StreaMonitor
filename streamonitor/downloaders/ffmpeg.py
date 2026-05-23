import errno
import os
import subprocess
import sys

import requests.cookies
from threading import Thread
from parameters import (
    DEBUG,
    SEGMENT_TIME,
    CONTAINER,
    FILENAME_TIME_FORMAT,
    FFMPEG_PATH,
    FFMPEG_READRATE,
)


def getVideoFfmpeg(self, url, filename, audio_url=None):
    cmd = [FFMPEG_PATH, "-user_agent", self.headers["User-Agent"]]

    if type(self.cookies) is requests.cookies.RequestsCookieJar:
        cookies_text = ""
        for cookie in self.cookies:
            cookies_text += (
                cookie.name
                + "="
                + cookie.value
                + "; path="
                + cookie.path
                + "; domain="
                + cookie.domain
                + "\n"
            )
        if len(cookies_text) > 10:
            cookies_text = cookies_text[:-1]
        cmd.extend(["-cookies", cookies_text])

    if FFMPEG_READRATE:
        cmd.extend(["-readrate", f"{FFMPEG_READRATE!s}"])

    # Build input parameters
    # When a separate audio URL is provided, use dual-input with stream mapping
    cmd.extend(
        [
            "-max_reload",
            "20",
            "-seg_max_retry",
            "20",
            "-m3u8_hold_counters",
            "20",
            "-i",
            url,
        ]
    )
    if audio_url:
        self.logger.info(f"Dual-input mode: video + audio separate streams")
        cmd.extend(["-i", audio_url, "-map", "0:v", "-map", "1:a"])
    else:
        cmd.extend(["-map", "0"])

    cmd.extend(["-c:a", "copy", "-c:v", "copy"])

    # Add container-specific ffmpeg arguments
    container_args = []
    if CONTAINER == "m4v":
        container_args = ["-f", "mpegts"]

    suffix = ""
    if hasattr(self, "filename_extra_suffix"):
        suffix = self.filename_extra_suffix

    if SEGMENT_TIME is not None:
        username = filename.rsplit("-", maxsplit=2)[0]
        cmd.extend(
            [
                "-f",
                "segment",
                "-reset_timestamps",
                "1",
                "-segment_time",
                str(SEGMENT_TIME),
                "-strftime",
                "1",
            ]
        )
        cmd.extend(container_args)
        cmd.append(f"{username}-{FILENAME_TIME_FORMAT}{suffix}.{CONTAINER}")
    else:
        cmd.extend(container_args)
        cmd.append(os.path.splitext(filename)[0] + suffix + "." + CONTAINER)

    class _Stopper:
        def __init__(self):
            self.stop = False

        def pls_stop(self):
            self.stop = True

    stopping = _Stopper()
    error = False

    def execute():
        nonlocal error
        try:
            stderr = (
                open(filename + ".stderr.log", "w+") if DEBUG else subprocess.DEVNULL
            )
            startupinfo = None
            if sys.platform == "win32":
                startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            process = subprocess.Popen(
                args=cmd,
                stdin=subprocess.PIPE,
                stderr=stderr,
                stdout=subprocess.DEVNULL,
                startupinfo=startupinfo,
            )
        except OSError as e:
            if e.errno == errno.ENOENT:
                self.logger.error("FFMpeg executable not found!")
                error = True
                return
            else:
                self.logger.error("Got OSError, errno: " + str(e.errno))
                error = True
                return

        while process.poll() is None:
            if stopping.stop:
                process.communicate(b"q")
                break
            try:
                process.wait(1)
            except subprocess.TimeoutExpired:
                pass

        if process.returncode and process.returncode != 0 and process.returncode != 255:
            self.logger.error(
                "The process exited with an error. Return code: "
                + str(process.returncode)
            )
            error = True
            return

    thread = Thread(target=execute)
    thread.start()
    self.stopDownload = lambda: stopping.pls_stop()
    thread.join()
    self.stopDownload = None
    return not error
