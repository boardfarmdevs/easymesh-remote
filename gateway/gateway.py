from __future__ import annotations

import argparse
import asyncio
import collections
import json
import logging
import signal
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
from multidict import CIMultiDict
from yarl import URL

from remote_state import Sessions, password_hash, password_matches


HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "content-length"}
ASSETS = Path(__file__).with_name("web")
LOGGER = logging.getLogger("easymesh.remote")
# The portal's files: the welcome and workspace page, and the tile that frames one view on its own origin.
PAGES = {"": "index.html", "tile": "tile.html"}
FILES = {"app.js", "app.css", "tile.js", "icon.svg"}
VIEWS = {
    "topology": ("Controller", "The EasyMesh controller's interface and API: topology, clients and steering."),
    "console": ("Console NG", "The RF medium, live and read-only: every path between radios and clients."),
    "room": ("Room", "The interactive room: move clients, play scenarios, watch the optimizer converge."),
}
ROLES = {"operator", "admin"}


def filtered_headers(headers):
    excluded = HOP_HEADERS | {entry.strip().lower() for entry in headers.get("Connection", "").split(",")}
    return CIMultiDict((name, value) for name, value in headers.items() if name.lower() not in excluded)


class Gateway:
    def __init__(self, config, sessions=None):
        self.config = config
        self.sessions = sessions or Sessions(
            Path(config["state_directory"]) / "sessions.sqlite3", config["idle_seconds"],
            config["maximum_seconds"], config["handoff_seconds"])
        self.cookie = "__Host-easymesh-" + config["lab"]
        self.client = None
        self.login_attempts = collections.deque()
        self.login_limit = asyncio.Semaphore(2)
        self.requests = asyncio.Semaphore(64)
        self.dummy_password = password_hash("not-a-real-account")

    def application(self, service):
        application = web.Application(client_max_size=2 * 1024 * 1024)

        async def handler(request):
            return await self.handle(request, service)

        application.router.add_route("*", "/{path:.*}", handler)
        return application

    def session_token(self, request):
        return request.cookies.get(self.cookie, "")

    def origins(self):
        return {name: "https://" + self.config["hostname"] + ("" if settings["public_port"] == 443 else
                ":" + str(settings["public_port"])) for name, settings in self.config["services"].items()}

    def frame_ancestors(self):
        # Each view keeps its own origin; the lab's page frames it from any of the lab's origins.
        return "frame-ancestors " + " ".join(self.origins().values())

    def account(self, username):
        """The password hash and role of an account, or (None, None). Read at each use: revocation is immediate."""
        entry = json.loads(Path(self.config["users_file"]).read_text()).get(username)
        if isinstance(entry, str):
            return entry, "operator"
        if isinstance(entry, dict) and isinstance(entry.get("password"), str) and entry.get("role", "operator") in ROLES:
            return entry["password"], entry.get("role", "operator")
        return None, None

    def validate_origin(self, request, service):
        expected = self.origins()[service]
        if request.host != urlsplit(expected).netloc:
            raise web.HTTPForbidden(text="Unexpected public hostname or port.")
        origin = request.headers.get("Origin")
        if origin and origin != expected:
            raise web.HTTPForbidden(text="Cross-origin access is not allowed.")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin != expected:
            raise web.HTTPForbidden(text="Writes require the service's HTTPS Origin header.")
        if request.headers.get("Sec-Fetch-Site") == "cross-site":
            raise web.HTTPForbidden(text="Cross-site access is not allowed.")

    async def handle(self, request, service):
        self.validate_origin(request, service)
        if request.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise web.HTTPUnsupportedMediaType(text="Compressed request bodies are not supported.")
        try:
            if request.path.startswith("/_remote/"):
                return await self.portal(request, service)
            token = self.session_token(request)
            if not self.sessions.status(token)["mine"]:
                if request.method == "GET" and "text/html" in request.headers.get("Accept", ""):
                    raise web.HTTPFound("/_remote/")
                return web.json_response({"error": "exclusive_session_required", "portal": "/_remote/"},
                                         status=423, headers={"Cache-Control": "no-store"})
            async with self.requests:
                if not self.sessions.status(token)["mine"]:
                    raise web.HTTPLocked(text="Session expired while waiting for the gateway.")
                return await self.forward_with_expiry(request, service, token)
        except web.HTTPException:
            raise
        except (asyncio.TimeoutError, OSError, ClientError) as error:
            LOGGER.warning("upstream unavailable service=%s type=%s", service, type(error).__name__)
            raise web.HTTPBadGateway(text="The lab service is unavailable; the reservation is unchanged.") from error

    def public_status(self, token, service):
        status = self.sessions.status(token)
        card = self.config.get("card", {})
        lab = {"title": card.get("title") or self.config["lab"], "summary": card.get("summary", "")}
        if not status["authenticated"]:
            status = {"authenticated": False, "mine": False}
        else:
            status["role"] = self.account(status["username"])[1] or "operator"
            if status["role"] == "admin":
                status["signed_in"] = self.sessions.signed_in()
            lab.update({key: card[key] for key in ("vm", "host", "built") if card.get(key)})
        origins = self.origins()
        return {**status, "schema": "easymesh.remote.session.v1", "lab": self.config["lab"], "service": service,
                "links": {name: origin + "/_remote/" for name, origin in origins.items()},
                "card": lab,
                "views": {name: {"title": VIEWS.get(name, (name, ""))[0], "summary": VIEWS.get(name, (name, ""))[1],
                                 "origin": origin} for name, origin in origins.items()},
                "rules": {key: self.config[key] for key in ("idle_seconds", "maximum_seconds", "handoff_seconds")}}

    def asset(self, suffix):
        response = web.FileResponse(ASSETS / PAGES.get(suffix, suffix))
        response.headers.update({"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                 "X-Content-Type-Options": "nosniff"})
        base = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; object-src 'none'; " \
               "base-uri 'none'; form-action 'self'; "
        if suffix == "tile":
            # Framed by the lab's page from any of its origins; frames its own view only.
            response.headers["Content-Security-Policy"] = base + "frame-src 'self'; " + self.frame_ancestors()
        else:
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Content-Security-Policy"] = base + "frame-src " + " ".join(
                origin + "/_remote/tile" for origin in self.origins().values()) + "; frame-ancestors 'none'"
        return response

    async def portal(self, request, service):
        suffix = request.path.removeprefix("/_remote/")
        if request.method == "GET" and (suffix in PAGES or suffix in FILES):
            return self.asset(suffix)
        token = self.session_token(request)
        if request.method == "GET" and suffix == "status":
            return web.json_response(self.public_status(token, service), headers={"Cache-Control": "no-store"})
        if request.method != "POST" or suffix not in {"login", "acquire", "activity", "release", "logout", "admin"}:
            raise web.HTTPNotFound()
        if request.content_type != "application/json" or request.content_length is None or request.content_length > 4096:
            raise web.HTTPBadRequest(text="A small JSON body is required.")
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError()
        except (ValueError, UnicodeError) as error:
            raise web.HTTPBadRequest(text="Invalid JSON object.") from error
        if suffix == "login":
            username = str(body.get("username", ""))[:80]
            password = str(body.get("password", ""))
            now = time.monotonic()
            while self.login_attempts and self.login_attempts[0][0] < now - 60:
                self.login_attempts.popleft()
            if len(password) > 512 or len(self.login_attempts) >= 20 or sum(
                    entry[1] == username for entry in self.login_attempts) >= 5:
                raise web.HTTPTooManyRequests(text="Too many sign-in attempts; wait one minute.",
                                             headers={"Retry-After": "60"})
            self.login_attempts.append((now, username))
            encoded = self.account(username)[0]
            async with self.login_limit:
                matched = await asyncio.to_thread(password_matches, password, encoded or self.dummy_password)
            if not encoded or not matched or self.account(username)[0] != encoded:
                LOGGER.warning("login rejected")
                raise web.HTTPUnauthorized(text="Invalid username or password.")
            token = self.sessions.login(username)
            response = web.json_response(self.public_status(token, service))
            response.set_cookie(self.cookie, token, secure=True, httponly=True, samesite="Strict", path="/", max_age=8 * 3600)
            response.headers["Cache-Control"] = "no-store"
            LOGGER.info("login user=%s", username)
            return response
        status = self.sessions.status(token)
        if not status["authenticated"]:
            raise web.HTTPUnauthorized(text="Sign in first.")
        try:
            if suffix == "acquire":
                if not status["busy"] and not status["handoff_remaining"] and not status["maintenance"]:
                    await self.check_room_free()
                self.sessions.acquire(token)
            elif suffix == "activity":
                self.sessions.activity(token)
            elif suffix == "release":
                self.sessions.release(token)
            elif suffix == "logout":
                self.sessions.logout(token)
            elif suffix == "admin":
                if self.account(status["username"])[1] != "admin":
                    raise PermissionError("Only an administrator can do this.")
                action = body.get("action")
                if action == "release":
                    self.sessions.release(force=True)
                elif action in {"maintenance-on", "maintenance-off"}:
                    self.sessions.set_maintenance(action == "maintenance-on")
                else:
                    raise web.HTTPBadRequest(text="Unknown administrative action.")
                suffix = "admin " + action
        except BlockingIOError as error:
            raise web.HTTPConflict(text=str(error)) from error
        except PermissionError as error:
            raise web.HTTPForbidden(text=str(error)) from error
        if suffix != "activity":
            LOGGER.info("session %s user=%s", suffix, status["username"])
        response = web.json_response(self.public_status(token, service), headers={"Cache-Control": "no-store"})
        if suffix == "logout":
            # del_cookie takes these attributes only from aiohttp 3.10; Ubuntu ships 3.8 and 3.9.
            response.set_cookie(self.cookie, "", max_age=0, expires="Thu, 01 Jan 1970 00:00:00 GMT",
                                path="/", secure=True, httponly=True, samesite="Strict")
        return response

    async def check_room_free(self):
        url = self.config["services"]["room"]["upstream"] + "/api/demo/interactions"
        async with self.client.get(url, timeout=ClientTimeout(total=5), allow_redirects=False) as response:
            if response.status != 200:
                raise web.HTTPServiceUnavailable(text="Room availability cannot be verified; acquisition refused.")
            body = bytearray()
            async for chunk in response.content.iter_any():
                body.extend(chunk)
                if len(body) > 2 * 1024 * 1024:
                    raise web.HTTPServiceUnavailable(text="Room availability response is too large.")
            try:
                snapshot = json.loads(body)
                lease = snapshot["lease"]
                held = lease["held"]
                if not isinstance(held, bool) or snapshot.get("schema") != "easymesh.room-demo.interactions.v1" or snapshot.get("enabled") is not True:
                    raise ValueError("unverified room state")
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                raise web.HTTPServiceUnavailable(text="Room lease status is unavailable.") from error
            if held:
                raise web.HTTPConflict(text="The room already has a local operator. Release that room lease first.")

    async def forward_with_expiry(self, request, service, token):
        proxy = asyncio.create_task(self.forward(request, service))

        async def watch():
            while self.sessions.status(token)["mine"]:
                await asyncio.sleep(1)

        watcher = asyncio.create_task(watch())
        try:
            completed, _pending = await asyncio.wait({proxy, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if proxy in completed:
                return await proxy
            if request.transport:
                request.transport.close()
            raise web.HTTPLocked(text="The exclusive session ended.")
        finally:
            for task in (proxy, watcher):
                if not task.done():
                    task.cancel()
            await asyncio.gather(proxy, watcher, return_exceptions=True)

    def upstream_headers(self, request):
        headers = filtered_headers(request.headers)
        for name in list(headers):
            if name.lower().startswith(("tailscale-", "x-forwarded-", "sec-websocket-")) or name.lower() in {"forwarded", "authorization"}:
                headers.popall(name, None)
        cookies = SimpleCookie()
        cookies.load(request.headers.get("Cookie", ""))
        cookies.pop(self.cookie, None)
        headers.popall("Cookie", None)
        if cookies:
            headers["Cookie"] = "; ".join(value.OutputString() for value in cookies.values())
        headers["X-Forwarded-Proto"] = "https"
        headers["X-Forwarded-Host"] = request.host
        return headers

    async def forward(self, request, service):
        upstream = self.config["services"][service]["upstream"]
        target = URL(upstream + request.raw_path, encoded=True)
        headers = self.upstream_headers(request)
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self.websocket(request, target, headers)
        body = await request.read()
        async with self.client.request(request.method, target, headers=headers, data=body or None,
                                       allow_redirects=False) as response:
            outgoing = filtered_headers(response.headers)
            outgoing["Cache-Control"] = "no-store"
            outgoing["Referrer-Policy"] = "same-origin"
            # The lab's page frames each view (in a tile on the view's own origin) from any of the lab's
            # origins; an upstream frame-ancestors or X-Frame-Options (Console NG: 'none') would forbid it.
            outgoing.popall("X-Frame-Options", None)
            for policy in outgoing.popall("Content-Security-Policy", []):
                kept = "; ".join(part.strip() for part in policy.split(";")
                                 if part.strip() and part.split()[0].lower() != "frame-ancestors")
                if kept:
                    outgoing.add("Content-Security-Policy", kept)
            outgoing.add("Content-Security-Policy", self.frame_ancestors())
            outgoing.popall("Set-Cookie", None)
            for value in response.headers.getall("Set-Cookie", []):
                cookies = SimpleCookie()
                cookies.load(value)
                if self.cookie not in cookies:
                    outgoing.add("Set-Cookie", value)
            location = outgoing.get("Location", "")
            if location == upstream or location.startswith(upstream + "/"):
                outgoing["Location"] = self.origins()[service] + location[len(upstream):]
            downstream = web.StreamResponse(status=response.status, headers=outgoing)
            await downstream.prepare(request)
            async for chunk in response.content.iter_any():
                await downstream.write(chunk)
            await downstream.write_eof()
            return downstream

    async def websocket(self, request, target, headers):
        protocols = tuple(entry.strip() for entry in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if entry.strip())
        async with self.client.ws_connect(target, headers=headers, protocols=protocols, heartbeat=25,
                                          max_msg_size=2 * 1024 * 1024) as upstream:
            downstream = web.WebSocketResponse(protocols=[upstream.protocol] if upstream.protocol else [],
                                               heartbeat=25, max_msg_size=2 * 1024 * 1024)
            await downstream.prepare(request)

            async def copy_messages(source, destination):
                async for message in source:
                    if message.type == WSMsgType.TEXT:
                        await destination.send_str(message.data)
                    elif message.type == WSMsgType.BINARY:
                        await destination.send_bytes(message.data)
                    else:
                        break

            tasks = [asyncio.create_task(copy_messages(upstream, downstream)),
                     asyncio.create_task(copy_messages(downstream, upstream))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                await downstream.close()
            return downstream


async def run(config):
    gateway = Gateway(config)
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stopped.set)
    runners = []
    async with ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False,
                             timeout=ClientTimeout(total=None, sock_connect=5), trust_env=False) as client:
        gateway.client = client
        try:
            for service, settings in config["services"].items():
                runner = web.AppRunner(gateway.application(service), access_log=None)
                await runner.setup()
                runners.append(runner)
                await web.TCPSite(runner, "127.0.0.1", settings["local_port"]).start()
            LOGGER.info("gateway ready lab=%s", config["lab"])
            await stopped.wait()
        finally:
            for runner in runners:
                await runner.cleanup()


def main():
    parser = argparse.ArgumentParser(description="Exclusive-session HTTPS backend for Tailscale Serve/Funnel.")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    asyncio.run(run(json.loads(Path(args.config).read_text())))


if __name__ == "__main__":
    main()
