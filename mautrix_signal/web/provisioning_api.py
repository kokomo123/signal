# mautrix-signal - A Matrix-Signal puppeting bridge
# Copyright (C) 2020 Tulir Asokan
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

from typing import TYPE_CHECKING
import asyncio
import json
import logging

from aiohttp import web
from attr import asdict

from mausignald.errors import InternalError, TimeoutException
from mausignald.types import Account, Address
from mautrix.types import UserID
from mautrix.util.bridge_state import BridgeState, BridgeStateEvent
from mautrix.util.logging import TraceLogger

from .. import user as u

if TYPE_CHECKING:
    from ..__main__ import SignalBridge


class ProvisioningAPI:
    log: TraceLogger = logging.getLogger("mau.web.provisioning")
    app: web.Application
    bridge: "SignalBridge"

    def __init__(self, bridge: "SignalBridge", shared_secret: str) -> None:
        self.bridge = bridge
        self.app = web.Application()
        self.shared_secret = shared_secret

        # Whoami
        self.app.router.add_get("/v1/api/whoami", self.status)
        self.app.router.add_get("/v2/whoami", self.status)

        # Logout
        self.app.router.add_options("/v1/api/logout", self.login_options)
        self.app.router.add_post("/v1/api/logout", self.logout)
        self.app.router.add_options("/v2/logout", self.login_options)
        self.app.router.add_post("/v2/logout", self.logout)

        # Link API (will be deprecated soon)
        self.app.router.add_options("/v1/api/link", self.login_options)
        self.app.router.add_options("/v1/api/link/wait", self.login_options)
        self.app.router.add_post("/v1/api/link", self.link)
        self.app.router.add_post("/v1/api/link/wait", self.link_wait)

        # New Login API
        self.app.router.add_options("/v2/link/new", self.login_options)
        self.app.router.add_options("/v2/link/wait/scan", self.login_options)
        self.app.router.add_options("/v2/link/wait/account", self.login_options)
        self.app.router.add_post("/v2/link/new", self.link_new)
        self.app.router.add_post("/v2/link/wait/scan", self.link_wait_for_scan)
        self.app.router.add_post("/v2/link/wait/account", self.link_wait_for_account)

    @property
    def _acao_headers(self) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
        }

    @property
    def _headers(self) -> dict[str, str]:
        return {
            **self._acao_headers,
            "Content-Type": "application/json",
        }

    async def login_options(self, _: web.Request) -> web.Response:
        return web.Response(status=200, headers=self._headers)

    async def check_token(self, request: web.Request) -> "u.User":
        try:
            token = request.headers["Authorization"]
            token = token[len("Bearer ") :]
        except KeyError:
            raise web.HTTPBadRequest(
                text='{"error": "Missing Authorization header"}', headers=self._headers
            )
        except IndexError:
            raise web.HTTPBadRequest(
                text='{"error": "Malformed Authorization header"}', headers=self._headers
            )
        if token != self.shared_secret:
            raise web.HTTPForbidden(text='{"error": "Invalid token"}', headers=self._headers)
        try:
            user_id = request.query["user_id"]
        except KeyError:
            raise web.HTTPBadRequest(
                text='{"error": "Missing user_id query param"}', headers=self._headers
            )

        if not self.bridge.signal.is_connected:
            await self.bridge.signal.wait_for_connected()

        return await u.User.get_by_mxid(UserID(user_id))

    async def status(self, request: web.Request) -> web.Response:
        user = await self.check_token(request)
        data = {
            "permissions": user.permission_level,
            "mxid": user.mxid,
            "signal": None,
        }
        if await user.is_logged_in():
            try:
                profile = await self.bridge.signal.get_profile(
                    username=user.username, address=Address(number=user.username)
                )
            except Exception as e:
                self.log.exception(f"Failed to get {user.username}'s profile for whoami")

                auth_failed = "org.whispersystems.signalservice.api.push.exceptions.AuthorizationFailedException"
                if isinstance(e, InternalError) and auth_failed in e.data.get("exceptions", []):
                    await user.push_bridge_state(BridgeStateEvent.BAD_CREDENTIALS, error=str(e))

                data["signal"] = {
                    "number": user.username,
                    "ok": False,
                    "error": str(e),
                }
            else:
                addr = profile.address if profile else None
                number = addr.number if addr else None
                uuid = addr.uuid if addr else None
                data["signal"] = {
                    "number": number or user.username,
                    "uuid": str(uuid or user.uuid or ""),
                    "name": profile.name if profile else None,
                    "ok": True,
                }
        return web.json_response(data, headers=self._acao_headers)

    async def _shielded_link(self, user: "u.User", session_id: str, device_name: str) -> Account:
        try:
            self.log.debug(f"Starting finish link request for {user.mxid} / {session_id}")
            account = await self.bridge.signal.finish_link(
                session_id=session_id, device_name=device_name, overwrite=True
            )
        except TimeoutException:
            self.log.warning(f"Timed out waiting for linking to finish (session {session_id})")
            raise
        except Exception:
            self.log.exception(
                f"Fatal error while waiting for linking to finish (session {session_id})"
            )
            raise
        else:
            await user.on_signin(account)
            return account

    async def _try_shielded_link(
        self, user: "u.User", session_id: str, device_name: str
    ) -> web.Response:
        try:
            account = await asyncio.shield(self._shielded_link(user, session_id, device_name))
        except asyncio.CancelledError:
            error_text = f"Client cancelled link wait request ({session_id}) before it finished"
            self.log.warning(error_text)
            raise web.HTTPInternalServerError(
                text=f'{{"error": "{error_text}"}}', headers=self._headers
            )
        except TimeoutException:
            raise web.HTTPBadRequest(
                text='{"error": "Signal linking timed out"}', headers=self._headers
            )
        except InternalError as ie:
            if "java.io.IOException" in ie.exceptions:
                raise web.HTTPBadRequest(
                    text='{"error": "Signald websocket disconnected before linking finished"}',
                    headers=self._headers,
                )
            raise web.HTTPInternalServerError(
                text='{"error": "Fatal error in Signal linking"}', headers=self._headers
            )
        except Exception:
            raise web.HTTPInternalServerError(
                text='{"error": "Fatal error in Signal linking"}', headers=self._headers
            )
        else:
            return web.json_response(account.address.serialize())

    # region Old Link API

    async def link(self, request: web.Request) -> web.Response:
        user = await self.check_token(request)

        if await user.is_logged_in():
            raise web.HTTPConflict(
                text="""{"error": "You're already logged in"}""", headers=self._headers
            )

        try:
            data = await request.json()
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text='{"error": "Malformed JSON"}', headers=self._headers)

        device_name = data.get("device_name", "Mautrix-Signal bridge")
        sess = await self.bridge.signal.start_link()

        user.command_status = {
            "action": "Link",
            "session_id": sess.session_id,
            "device_name": device_name,
        }

        self.log.debug(f"Returning linking URI for {user.mxid} / {sess.session_id}")
        return web.json_response({"uri": sess.uri}, headers=self._acao_headers)

    async def link_wait(self, request: web.Request) -> web.Response:
        user = await self.check_token(request)
        if not user.command_status or user.command_status["action"] != "Link":
            raise web.HTTPBadRequest(
                text='{"error": "No Signal linking started"}', headers=self._headers
            )
        session_id = user.command_status["session_id"]
        device_name = user.command_status["device_name"]
        return await self._try_shielded_link(user, session_id, device_name)

    # endregion

    # region New Link API

    async def _get_request_data(self, request: web.Request) -> tuple[u.User, web.Response]:
        user = await self.check_token(request)
        if await user.is_logged_in():
            error_text = """{"error": "You're already logged in"}"""
            raise web.HTTPConflict(text=error_text, headers=self._headers)

        try:
            return user, (await request.json())
        except json.JSONDecodeError:
            raise web.HTTPBadRequest(text='{"error": "Malformed JSON"}', headers=self._headers)

    async def link_new(self, request: web.Request) -> web.Response:
        """
        Starts a new link session.

        Params: none

        Returns a JSON object with the following fields:

        * session_id: a session ID that should be used for all future link-related commands
          (wait_for_scan and wait_for_account).
        * uri: a URI that should be used to display the QR code.
        """
        user, _ = await self._get_request_data(request)
        self.log.debug(f"Getting session ID and link URI for {user.mxid}")
        sess = await self.bridge.signal.start_link()
        self.log.debug(f"Returning session ID and link URI for {user.mxid} / {sess.session_id}")
        return web.json_response(asdict(sess), headers=self._acao_headers)

    async def link_wait_for_scan(self, request: web.Request) -> web.Response:
        """
        Waits for the QR code associated with the provided session ID to be scanned.

        Params: a JSON object with the following field:

        * session_id: a session ID that you got from a call to /link/v2/new.
        """
        _, request_data = await self._get_request_data(request)
        try:
            session_id = request_data["session_id"]
        except KeyError:
            error_text = '{"error": "session_id not provided"}'
            raise web.HTTPBadRequest(text=error_text, headers=self._headers)

        try:
            await self.bridge.signal.wait_for_scan(session_id)
        except Exception as e:
            error_text = f"Failed waiting for scan. Error: {e}"
            self.log.exception(error_text)
            self.log.info(e.__class__)
            raise web.HTTPBadRequest(text=error_text, headers=self._headers)
        else:
            return web.json_response({}, headers=self._acao_headers)

    async def link_wait_for_account(self, request: web.Request) -> web.Response:
        """
        Waits for the link to the user's phone to complete.

        Params: a JSON object with the following fields:

        * session_id: a session ID that you got from a call to /link/v2/new.
        * device_name: the device name that will show up in Linked Devices on the user's device.

        Returns: a JSON object representing the user's account.
        """
        user, request_data = await self._get_request_data(request)
        try:
            session_id = request_data["session_id"]
            device_name = request_data.get("device_name", "Mautrix-Signal bridge")
        except KeyError:
            error_text = '{"error": "session_id not provided"}'
            raise web.HTTPBadRequest(text=error_text, headers=self._headers)

        return await self._try_shielded_link(user, session_id, device_name)

    async def logout(self, request: web.Request) -> web.Response:
        user = await self.check_token(request)
        if not await user.is_logged_in():
            raise web.HTTPNotFound(
                text="""{"error": "You're not logged in"}""", headers=self._headers
            )
        await user.logout()
        return web.json_response({}, headers=self._acao_headers)
