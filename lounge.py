"""pyytlounge's YtLoungeApi, connecting with a remote id of its own.

pyytlounge 3.4.0's connect() sends the TV's screen id as the remote's device "id". iSponsorBlockTV
does the same, so every remote paired to a TV claims to be one device: YouTube then hands one
session's events and commands to the other, and ours stopped hearing back (speed and caption
changes never acknowledged) once iSponsorBlockTV joined a TV. Two people pairing the same TV here
clashed the same way. This sends a stable id per (person, TV) instead.

connect() below is pyytlounge 3.4.0's, unchanged but for "id". tests/test_lounge.py fails if a
pyytlounge upgrade changes the original, so an automatic upgrade can't silently drop this.
"""
import uuid

from pyytlounge import YtLoungeApi
from pyytlounge.api import api_base
from pyytlounge.exceptions import NotLinkedException
from pyytlounge.util import as_aiter

REMOTE_ID_NAMESPACE = uuid.UUID("6f1b0d55-2a43-4a8e-9a57-0c3d4c0e7f21")


class LoungeApi(YtLoungeApi):
    def __init__(self, device_name: str, listener, remote_key: str):
        super().__init__(device_name, listener)
        self.remote_key = remote_key  # who this session is for (a Canopy person id)

    @property
    def remote_id(self) -> str:
        """Stable across restarts and reconnects, distinct per person and TV."""
        return str(uuid.uuid5(REMOTE_ID_NAMESPACE, f"{self.remote_key}:{self.auth.screen_id}"))

    async def connect(self) -> bool:
        if not self.linked():
            raise NotLinkedException("Not linked")

        connect_body = {
            "app": "web",
            "mdx-version": "3",
            "name": self.device_name,
            "id": self.remote_id,
            "device": "REMOTE_CONTROL",
            "capabilities": "que,dsdtr,atp,vsp",
            "magnaKey": "cloudPairedDevice",
            "ui": "false",
            "deviceContext": "user_agent=dunno&window_width_points=&window_height_points=&os_name=android&ms=",
            "theme": "cl",
            "loungeIdToken": self.auth.lounge_id_token,
        }
        connect_url = f"{api_base}/bc/bind?RID=1&VER=8&CVER=1&auth_failure_option=send_error"
        async with self._required_session.post(url=connect_url, data=connect_body) as resp:
            try:
                text = await resp.text()
                if resp.status == 401:
                    self._lounge_token_expired()
                    return False

                if resp.status != 200:
                    self._logger.warning("Unknown reply to connect %i %s", resp.status, resp.reason)
                    return False
                lines = text.splitlines()
                async for events in self._parse_event_chunks(as_aiter(lines)):
                    await self._process_events(events)
                self._command_offset = 1
                return self.connected()
            except:
                self._logger.exception(
                    "Handle connect failed, status %s reason %s",
                    resp.status,
                    resp.reason,
                )
                raise
