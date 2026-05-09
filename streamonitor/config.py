import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from ffmpy import FFRuntimeError

from streamonitor.bot import Bot
from streamonitor.log import Logger
from parameters import (
    DOWNLOADS_DIR,
    DEBUG,
    FILENAME_TIME_FORMAT,
    FFMPEG_PATH,
    RECORDING_TRACKING_FILE,
    TEMP_DOWNLOAD_ENABLED,
    TEMP_FOLDER,
    CONTAINER,
    SEGMENT_TIME,
)

logger = Logger('[CONFIG]').get_logger()
config_loc = "config.json"


def recover_recordings():
    """Power failure recovery: move remaining temp files to output paths and clean stale entries.
    
    Handles two types of temporary files:
    1. StripChat HLS downloads: intermediate .tmp.ts that needs FFmpeg transcode to .m4v/.mp4
    2. Direct temp files: .m4v/.mp4 in temp folder that need to be moved to output
    """
    if not TEMP_DOWNLOAD_ENABLED:
        return
    
    logger.info("Starting power failure recovery")
    
    # Step 1: Recover orphaned .tmp.ts files from StripChat HLS downloads
    recover_tmp_ts_files()
    
    # Step 2: Recover entries from recordings.json
    recover_tracked_recordings()
    
    logger.info("Power failure recovery complete")


def recover_tmp_ts_files():
    """Scan temp folder for orphaned .tmp.ts files and attempt to transcode/move them.
    
    StripChat HLS downloads create intermediate .tmp.ts files that need FFmpeg
    transcode to the final container format (.m4v or .mp4) before they can be moved.
    """
    if not TEMP_FOLDER:
        return
    
    temp_folder = TEMP_FOLDER
    if not os.path.isabs(temp_folder):
        temp_folder = os.path.join(DOWNLOADS_DIR, temp_folder)
    
    if not os.path.exists(temp_folder):
        return
    
    # Find all .tmp.ts files in temp folder
    tmp_ts_files = []
    try:
        for entry in os.scandir(temp_folder):
            if entry.is_file() and entry.name.endswith('.tmp.ts'):
                tmp_ts_files.append(entry.path)
    except OSError as e:
        logger.error(f"Failed to scan temp folder: {e}")
        return
    
    if not tmp_ts_files:
        return
    
    logger.info(f"Found {len(tmp_ts_files)} orphaned .tmp.ts file(s)")
    
    for tmp_ts_path in tmp_ts_files:
        try:
            # Parse username and timestamp from filename
            # Expected format: username-YYYY-MM-DD_HH-MM-SS.tmp.ts
            basename = os.path.basename(tmp_ts_path)
            match = re.match(r'^(.+?)-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.tmp\.ts$', basename)
            if not match:
                logger.warning(f"Could not parse filename pattern: {basename}")
                continue
            
            username = match.group(1)
            timestamp = re.search(r'(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})', basename).group(1)
            
            # Construct output path
            output_filename = os.path.join(
                DOWNLOADS_DIR, "StripChat", username,
                f"{username}-{timestamp}.{CONTAINER}"
            )
            
            logger.info(f"Attempting to recover: {tmp_ts_path}")
            
            # Transcode .tmp.ts to final container
            if transcode_tmp_ts(tmp_ts_path, output_filename):
                logger.info(f"Successfully recovered: {output_filename}")
            else:
                logger.error(f"Failed to transcode: {tmp_ts_path}")
                
        except Exception as e:
            logger.error(f"Error processing {tmp_ts_path}: {e}")


def transcode_tmp_ts(tmp_ts_path, output_filename):
    """Transcode a .tmp.ts file to the final container format using FFmpeg.
    
    This replicates the FFmpeg logic from hls.py for recovery purposes.
    
    Returns True if transcode and move succeeded, False otherwise.
    """
    try:
        stdout = (
            open(output_filename + ".postprocess_stdout.log", "w+")
            if DEBUG
            else subprocess.DEVNULL
        )
        stderr = (
            open(output_filename + ".postprocess_stderr.log", "w+")
            if DEBUG
            else subprocess.DEVNULL
        )
        
        # Build FFmpeg output options - same logic as hls.py
        output_str = "-c:a copy -c:v copy"
        if CONTAINER == "m4v":
            output_str += " -f mpegts"
        suffix = ""
        if SEGMENT_TIME is not None:
            output_str += f" -f segment -reset_timestamps 1 -segment_time {str(SEGMENT_TIME)}"
            # For segmented files, we'd need to handle multiple outputs
            # For recovery, we'll just create single output
            suffix = "_000"
            output_filename = output_filename[: -len("." + CONTAINER)] + "_000." + CONTAINER
        
        from ffmpy import FFmpeg
        
        ff = FFmpeg(
            executable=FFMPEG_PATH,
            inputs={tmp_ts_path: None},
            outputs={output_filename: output_str},
        )
        ff.run(stdout=stdout, stderr=stderr)
        
        # Clean up .tmp.ts after successful transcode
        os.remove(tmp_ts_path)
        
        # Create output directory and move to final location
        final_output = output_filename
        if "_000" in final_output:
            final_output = final_output.replace("_000", "")
        os.makedirs(os.path.dirname(final_output), exist_ok=True)
        if final_output != output_filename:
            shutil.move(output_filename, final_output)
        
        return True
        
    except FFRuntimeError as e:
        # Exit code 183 = SIGTERM - FFmpeg was interrupted mid-process
        # This means the .tmp.ts file is incomplete/corrupted, delete it
        if e.exit_code == 183:
            logger.warning(f"Corrupted .tmp.ts file (interrupted download): {tmp_ts_path} - removing")
            try:
                os.remove(tmp_ts_path)
            except OSError:
                pass
            return False  # Don't retry
        logger.error(f"FFmpeg transcode failed: {e}")
        return False
    except Exception as e:
        logger.error(f"Transcode error: {e}")
        return False


def recover_tracked_recordings():
    """Process entries from recordings.json - move temp files to output paths."""
    if not os.path.exists(RECORDING_TRACKING_FILE):
        return
    
    try:
        with open(RECORDING_TRACKING_FILE, "r") as f:
            tracking_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return
    
    if not tracking_data:
        return
    
    logger.info(f"Processing {len(tracking_data)} tracked recording(s)")
    
    recovered = []
    stale = []
    cutoff_time = datetime.now() - timedelta(hours=24)
    
    for recording in tracking_data:
        temp_filename = recording.get("temp_filename")
        output_filename = recording.get("output_filename")
        timestamp_str = recording.get("timestamp")
        
        # Check if temp file exists
        if not temp_filename or not os.path.exists(temp_filename):
            stale.append(recording)
            logger.debug(f"Temp file not found, marking stale: {temp_filename}")
            continue
        
        # Try to move the file
        try:
            os.makedirs(os.path.dirname(output_filename), exist_ok=True)
            shutil.move(temp_filename, output_filename)
            recovered.append(recording)
            logger.info(f"Recovered recording: {output_filename}")
        except Exception as e:
            logger.error(f"Failed to recover {temp_filename}: {e}")
            stale.append(recording)
    
    # Check for stale entries based on timestamp
    for recording in tracking_data:
        timestamp_str = recording.get("timestamp")
        if timestamp_str:
            try:
                timestamp = datetime.fromisoformat(timestamp_str)
                if timestamp < cutoff_time:
                    if recording not in stale:
                        stale.append(recording)
            except ValueError:
                if recording not in stale:
                    stale.append(recording)
    
    # Update tracking file - remove recovered and stale entries
    remaining = [r for r in tracking_data if r not in recovered and r not in stale]
    
    try:
        with open(RECORDING_TRACKING_FILE, "w") as f:
            json.dump(remaining, f, indent=4)
    except IOError as e:
        logger.error(f"Failed to update tracking file: {e}")
    
    if recovered:
        logger.info(f"Recovery complete: {len(recovered)} recording(s) moved, {len(stale)} entries cleaned")


def load_config():
    try:
        with open(config_loc, "r+") as f:
            return json.load(f)
    except FileNotFoundError:
        with open(config_loc, "w+") as f:
            json.dump([], f, indent=4)
            return []
    except Exception as e:
        print(e)
        sys.exit(1)


def save_config(config):
    try:
        with open(config_loc, "w+") as f:
            json.dump(config, f, indent=4)

        return True
    except Exception as e:
        print(e)
        sys.exit(1)


def loadStreamers():
    # Run power failure recovery before loading streamers
    recover_recordings()
    
    streamers = []
    for streamer in load_config():
        username = streamer["username"]
        site = streamer["site"]

        bot_class = Bot.str2site(site)
        if not bot_class:
            logger.warning(f'Unknown site: {site} (user: {username})')
            continue

        streamer_bot = bot_class.fromConfig(streamer)
        streamers.append(streamer_bot)
        streamer_bot.start()
        time.sleep(0.1)
    return streamers
