import json
import os
import shutil
import sys
import time
from datetime import datetime, timedelta

from streamonitor.bot import Bot
from streamonitor.log import Logger
from parameters import (
    RECORDING_TRACKING_FILE,
    TEMP_DOWNLOAD_ENABLED,
)

logger = Logger('[CONFIG]').get_logger()
config_loc = "config.json"


def recover_recordings():
    """Power failure recovery: move remaining temp files to output paths and clean stale entries."""
    if not TEMP_DOWNLOAD_ENABLED:
        return
    
    if not os.path.exists(RECORDING_TRACKING_FILE):
        return
    
    try:
        with open(RECORDING_TRACKING_FILE, "r") as f:
            tracking_data = json.load(f)
    except (json.JSONDecodeError, IOError):
        return
    
    if not tracking_data:
        return
    
    logger.info(f"Starting power failure recovery for {len(tracking_data)} recording(s)")
    
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
