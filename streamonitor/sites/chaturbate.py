import re
import requests
from streamonitor.bot import Bot
from streamonitor.enums import Status, Gender


class Chaturbate(Bot):
    site = 'Chaturbate'
    siteslug = 'CB'
    bulk_update = True

    _GENDER_MAP = {
        'f': Gender.FEMALE,
        'm': Gender.MALE,
        's': Gender.TRANS,
        'c': Gender.BOTH,
    }

    def __init__(self, username):
        super().__init__(username)
        self.sleep_on_offline = 30
        self.sleep_on_error = 60
    
    def getWebsiteURL(self):
        return "https://www.chaturbate.com/" + self.username
    
    def getVideoUrl(self):
        if self.bulk_update:
            self.getStatus()
        url = self.lastInfo['url']
        if not url:
            return None
        if self.lastInfo.get('cmaf_edge'):
            url = url.replace('playlist.m3u8', 'playlist_sfm4s.m3u8')
            url = re.sub('live-.+amlst', 'live-c-fhls/amlst', url)

        return self.getWantedResolutionPlaylist(url)
    
    @staticmethod
    def _parseStatus(status):
        if status == "public":
            return Status.PUBLIC
        elif status in ["private", "hidden"]:
            return Status.PRIVATE
        else:
            return Status.OFFLINE

    def getStatus(self):
        # Hybrid Approach: First check via /api/biocontext/, then get stream URL
        try:
            # Step 1: Quick status check via biocontext API
            bio_url = f"https://chaturbate.com/api/biocontext/{self.username}/"
            bio_response = self.session.get(bio_url, timeout=10)
            
            # Check if response is JSON (not HTML redirect/error page)
            content_type = bio_response.headers.get('Content-Type', '')
            if 'application/json' not in content_type:
                # Not a JSON response - model is offline or doesn't exist
                self.lastInfo = {'url': None, 'room_status': None}
                return Status.OFFLINE
            
            bio_data = bio_response.json()
            room_status = bio_data.get('room_status')
            if not room_status:
                self.lastInfo = {'url': None, 'room_status': None}
                return Status.OFFLINE
            
            # Step 2: If online, get stream URL
            hls_headers = {"X-Requested-With": "XMLHttpRequest"}
            hls_data = {"room_slug": self.username, "bandwidth": "high"}
            
            hls_response = self.session.post(
                "https://chaturbate.com/get_edge_hls_url_ajax/",
                headers=hls_headers,
                data=hls_data
            )
            self.lastInfo = hls_response.json()
            
            status = self._parseStatus(self.lastInfo.get('room_status', room_status))
            if status == Status.PUBLIC and not self.lastInfo.get('url'):
                status = Status.RESTRICTED
                
        except requests.exceptions.RequestException:
            self.lastInfo = {'url': None, 'room_status': None}
            status = Status.RATELIMIT
        except (KeyError, ValueError, AttributeError):
            self.lastInfo = {'url': None, 'room_status': None}
            status = Status.ERROR

        self.ratelimit = status == Status.RATELIMIT
        return status

    @classmethod
    def getStatusBulk(cls, streamers):
        # Filter only Chaturbate streamers
        cb_streamers = [s for s in streamers if isinstance(s, Chaturbate)]
        if not cb_streamers:
            return

        session = requests.Session()
        session.headers.update(cls.headers)
        r = session.get("https://chaturbate.com/affiliates/api/onlinerooms/?format=json&wm=DkfRj", timeout=10)

        try:
            data = r.json()
        except requests.exceptions.JSONDecodeError:
            print('Failed to parse JSON response')
            return

        data_map = {str(model['username']).lower(): model for model in data}

        for streamer in cb_streamers:
            model_data = data_map.get(streamer.username.lower())
            if not model_data:
                # Not in bulk list - use individual getStatus() for accurate check
                status = streamer.getStatus()
                streamer.setStatus(status)
                continue

            # Found in bulk list - may be online
            if model_data.get('gender'):
                streamer.gender = cls._GENDER_MAP.get(model_data.get('gender'))
            if model_data.get('country'):
                streamer.country = model_data.get('country', '').upper()

            bulk_status = cls._parseStatus(model_data['current_show'])

            # Hybrid: If bulk says PUBLIC, verify with individual API call
            if bulk_status == Status.PUBLIC:
                status = streamer.getStatus()  # This calls biocontext + HLS APIs
            else:
                status = bulk_status

            streamer.setStatus(status)
