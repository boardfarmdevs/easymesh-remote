from __future__ import annotations

import concurrent.futures
import copy
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
REMOTE = ROOT / "gateway"
sys.path.insert(0, str(REMOTE))
from remote_state import Sessions, password_hash, password_matches, token_hash
import manage as remote_manage


def configuration(directory):
    return {
        "lab": "test-lab", "hostname": "lab.test.ts.net", "vm": "test-lab",
        "state_directory": str(directory), "users_file": str(Path(directory) / "users.json"),
        "idle_seconds": 30, "maximum_seconds": 90, "handoff_seconds": 125,
        "services": {name: {"upstream": f"http://192.168.1.2:{21000 + offset}",
            "host_address": "192.168.1.2", "host_port": 21000 + offset,
            "guest_address": "10.0.0.2", "guest_port": 8888 + offset,
            "guest_addresses": ["10.0.0.2", "fd42::2"],
            "public_port": port, "local_port": 41000 + offset}
            for offset, (name, port) in enumerate(remote_manage.PUBLIC_PORTS.items())}}


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.store = Sessions(Path(self.directory.name) / "state.db", 30, 90, 125, lambda: self.now)
        self.alice = self.store.login("alice")
        self.bob = self.store.login("bob")

    def test_passwords_salted_and_verified(self):
        encoded = password_hash("long test password")
        self.assertTrue(password_matches("long test password", encoded))
        self.assertFalse(password_matches("wrong", encoded))
        self.assertFalse(password_matches("wrong", "malformed"))
        self.assertNotEqual(encoded, password_hash("long test password"))

    def test_explicit_acquire_and_cross_user_exclusion(self):
        self.assertFalse(self.store.status(self.alice)["busy"])
        self.assertTrue(self.store.acquire(self.alice)["mine"])
        with self.assertRaises(BlockingIOError):
            self.store.acquire(self.bob)
        with self.assertRaises(PermissionError):
            self.store.release(self.bob)
        with self.assertRaises(PermissionError):
            self.store.activity(self.bob)

    def test_same_username_different_browser_cannot_share(self):
        self.store.acquire(self.alice)
        other = self.store.login("alice")
        with self.assertRaises(BlockingIOError):
            self.store.acquire(other)

    def test_status_and_reacquire_do_not_extend_idle(self):
        self.store.acquire(self.alice)
        self.now += 20
        self.assertEqual(self.store.acquire(self.alice)["idle_remaining"], 10)
        self.now += 11
        self.assertFalse(self.store.status(self.alice)["mine"])
        self.assertEqual(self.store.status(self.alice)["handoff_remaining"], 125)

    def test_activity_has_fixed_maximum(self):
        self.store.acquire(self.alice)
        for _step in range(4):
            self.now += 20
            self.store.activity(self.alice)
        self.assertEqual(self.store.status(self.alice)["idle_remaining"], 10)
        self.now += 11
        with self.assertRaises(PermissionError):
            self.store.activity(self.alice)

    def test_restart_preserves_lock_and_deadlines(self):
        self.store.acquire(self.alice)
        self.now += 20
        reopened = Sessions(self.store.path, 30, 90, 125, lambda: self.now)
        self.assertTrue(reopened.status(self.alice)["mine"])
        self.assertEqual(reopened.status(self.alice)["idle_remaining"], 10)
        with self.assertRaises(BlockingIOError):
            reopened.acquire(self.bob)

    def test_release_waits_before_handoff(self):
        self.store.acquire(self.alice)
        self.store.release(self.alice)
        with self.assertRaises(BlockingIOError):
            self.store.acquire(self.bob)
        self.now += 126
        self.assertTrue(self.store.acquire(self.bob)["mine"])
        self.assertFalse(self.store.status(self.alice)["mine"])

    def test_logout_and_admin_release_revoke(self):
        self.store.acquire(self.alice)
        self.store.logout(self.alice)
        self.assertFalse(self.store.status(self.alice)["authenticated"])
        self.now += 126
        self.store.acquire(self.bob)
        self.store.release(force=True)
        self.assertFalse(self.store.status(self.bob)["mine"])
        self.assertEqual(self.store.status(self.bob)["handoff_remaining"], 125)

    def test_removed_user_loses_live_session(self):
        self.store.acquire(self.alice)
        self.store.revoke_user("alice")
        self.assertFalse(self.store.status(self.alice)["authenticated"])
        self.assertFalse(self.store.status(self.alice)["busy"])

    def test_expired_login_cannot_acquire(self):
        self.now += 8 * 3600
        with self.assertRaises(PermissionError):
            self.store.acquire(self.alice)

    def test_two_process_connections_have_exactly_one_winner(self):
        def attempt(token):
            other = Sessions(self.store.path, 30, 90, 125, lambda: self.now)
            try:
                return other.acquire(token)["mine"]
            except BlockingIOError:
                return False

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as workers:
            self.assertEqual(sum(workers.map(attempt, [self.alice, self.bob])), 1)

    def test_no_plaintext_token_in_database(self):
        self.store.acquire(self.alice)
        content = self.store.path.read_bytes()
        self.assertNotIn(self.alice.encode(), content)
        self.assertIn(token_hash(self.alice).encode(), content)

    def test_maintenance_revokes_owner_and_persists(self):
        self.store.acquire(self.alice)
        self.store.set_maintenance(True)
        self.assertFalse(self.store.status(self.alice)["mine"])
        self.now += 126
        reopened = Sessions(self.store.path, 30, 90, 125, lambda: self.now)
        with self.assertRaisesRegex(BlockingIOError, "maintenance"):
            reopened.acquire(self.bob)
        reopened.set_maintenance(False)
        self.assertTrue(reopened.acquire(self.bob)["mine"])


class SetupTests(unittest.TestCase):
    def publication(self, config, public=False):
        result = {"TCP": {}, "Web": {}, "AllowFunnel": {}}
        for settings in config["services"].values():
            port = str(settings["public_port"])
            endpoint = config["hostname"] + ":" + port
            result["TCP"][port] = {"HTTPS": True}
            result["Web"][endpoint] = {"Handlers": {"/": {"Proxy": remote_manage.expected_proxy(settings)}}}
            if public:
                result["AllowFunnel"][endpoint] = True
        return result

    def test_private_publish_protects_backends_before_exposure(self):
        with tempfile.TemporaryDirectory() as directory:
            config = configuration(directory)
            Path(config["users_file"]).write_text('{"alice": "test-only-hash"}')
            recorded = []
            with patch.object(remote_manage, "discover_services", return_value=config["services"]), \
                    patch.object(remote_manage, "read_command", side_effect=["{}", json.dumps(self.publication(config))]), \
                    patch.object(remote_manage, "command", side_effect=lambda *arguments: recorded.append(arguments)), \
                    patch.object(remote_manage, "firewall", side_effect=lambda *arguments: recorded.append(("firewall", True))), \
                    patch.object(remote_manage, "check_gateway", side_effect=lambda *arguments: recorded.append(("ready",))):
                remote_manage.publish(config, "private")
            expose = [entry for entry in recorded if entry[0] == "tailscale"]
            self.assertEqual(len(expose), 3)
            self.assertTrue(all(entry[1] == "serve" and "--bg" in entry for entry in expose))
            self.assertLess(recorded.index(("firewall", True)), recorded.index(("ready",)))
            self.assertLess(recorded.index(("ready",)), recorded.index(expose[0]))

    def test_public_unpublish_removes_only_owned_funnels(self):
        config = configuration("/tmp")
        state = self.publication(config, public=True)
        state["TCP"]["12345"] = {"TCPForward": "127.0.0.1:9999"}
        with patch.object(remote_manage, "read_command", return_value=json.dumps(state)), \
                patch.object(remote_manage, "command") as commands:
            remote_manage.unpublish(config)
        self.assertEqual(commands.call_count, 3)
        for entry in commands.call_args_list:
            self.assertEqual(entry.args[:2], ("tailscale", "funnel"))
            self.assertEqual(entry.args[-1], "off")
            self.assertNotIn("--https=12345", entry.args)

    def test_failed_publication_rolls_back_even_the_failed_port(self):
        with tempfile.TemporaryDirectory() as directory:
            config = configuration(directory)
            Path(config["users_file"]).write_text('{"alice": "test-only-hash"}')

            def execute(*arguments):
                if "--https=8443" in arguments:
                    raise subprocess.CalledProcessError(1, arguments)

            with patch.object(remote_manage, "discover_services", return_value=config["services"]), \
                    patch.object(remote_manage, "read_command", return_value="{}"), \
                    patch.object(remote_manage, "command", side_effect=execute), \
                    patch.object(remote_manage, "firewall"), patch.object(remote_manage, "check_gateway"), \
                    patch.object(remote_manage.subprocess, "run") as rollback:
                with self.assertRaises(subprocess.CalledProcessError):
                    remote_manage.publish(config, "public", confirmed=True)
            self.assertEqual([entry.args[0] for entry in rollback.call_args_list], [
                ["tailscale", "funnel", "--https=443", "off"],
                ["tailscale", "funnel", "--https=8443", "off"]])

    def test_firewall_is_narrow_and_precedes_dnat(self):
        config = configuration("/tmp")
        rules = remote_manage.firewall_rules(config, replace=True)
        self.assertIn("priority -110", rules)
        self.assertIn("ip daddr 192.168.1.2 tcp dport 21000 counter drop", rules)
        self.assertIn("ip daddr 10.0.0.2 tcp dport 8888 counter drop", rules)
        self.assertNotIn("flush ruleset", rules)
        self.assertNotIn("hook output", rules)
        self.assertIn("ip6 daddr fd42::2 tcp dport 8888 counter drop", rules)
        self.assertEqual(rules.count("counter drop"), 9)
        self.assertTrue(rules.startswith("delete table inet em_remote_"))

    def test_publication_refuses_other_app_and_path_handlers(self):
        config = configuration("/tmp")
        remote_manage.check_publication(config, {})
        current = {"TCP": {"443": {"HTTPS": True}}, "Web": {"lab.test.ts.net:443": {
            "Handlers": {"/": {"Proxy": "http://127.0.0.1:41000"}}}}}
        remote_manage.check_publication(config, current)
        for change in ("proxy", "path", "listener", "hostname"):
            altered = copy.deepcopy(current)
            if change == "proxy":
                altered["Web"]["lab.test.ts.net:443"]["Handlers"]["/"]["Proxy"] = "http://127.0.0.1:9999"
            elif change == "path":
                altered["Web"]["lab.test.ts.net:443"]["Handlers"]["/unprotected"] = {"Proxy": "http://127.0.0.1:8888"}
            elif change == "listener":
                altered["TCP"]["443"] = {"TCPForward": "localhost:1234"}
            else:
                altered["Web"]["other.test.ts.net:443"] = altered["Web"].pop("lab.test.ts.net:443")
            with self.subTest(change=change), self.assertRaises(ValueError):
                remote_manage.check_publication(config, altered)

    def test_public_needs_explicit_confirmation(self):
        with self.assertRaisesRegex(ValueError, "confirm-public"):
            remote_manage.publish(configuration("/tmp"), "public")

    def test_bad_endpoint_cannot_become_a_firewall_rule(self):
        for value in ("tcp:0.0.0.0:8080", "tcp:::1:8888", "udp:192.168.1.2:9999", "tcp:127.0.0.1:99999"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                remote_manage.parse_endpoint(value)

    def test_discovery_uses_existing_vm_ports(self):
        devices = {name: {"type": "proxy", "nat": "true", "listen": f"tcp:192.168.1.2:{26340 + offset}",
                          "connect": f"tcp:10.0.0.5:{8888 + offset}"}
                   for offset, name in enumerate(remote_manage.DEVICES.values())}
        state = {"network": {"eth0": {"addresses": [{"address": "10.0.0.5"}, {"address": "fd42::5"}]}}}
        with patch.object(remote_manage, "read_command", side_effect=[json.dumps({"expanded_devices": devices}), json.dumps(state)]):
            services = remote_manage.discover_services("demo-a", 40000)
        self.assertEqual(services["room"]["upstream"], "http://192.168.1.2:26342")
        self.assertEqual(services["console"]["public_port"], 8443)
        self.assertEqual(services["console"]["guest_addresses"], ["10.0.0.5", "fd42::5"])

    def test_portal_activity_excludes_automatic_polling(self):
        source = (REMOTE / "web/portal.js").read_text()
        self.assertIn("!event.isTrusted", source)
        self.assertIn("document.visibilityState !== 'visible'", source)
        self.assertIn("!document.hasFocus()", source)
        self.assertIn("setInterval(refresh, 5000)", source)
        self.assertNotIn("setInterval(activity", source)
        self.assertIn("elements.view.contentWindow.document", source)

    def test_firewall_in_isolated_user_and_network_namespaces(self):
        if not all(shutil.which(name) for name in ("unshare", "nft", "ip", "nsenter", "sysctl")):
            self.skipTest("install nftables, iproute2 and util-linux for isolated firewall integration")
        capability = subprocess.run(["unshare", "--user", "--map-root-user", "--net", "true"], capture_output=True)
        if capability.returncode:
            self.skipTest("unprivileged user/network namespaces unavailable; use manual remote denial checks")
        arguments = ["unshare", "--user", "--map-root-user", "--net", sys.executable,
                     str(ROOT / "tests/remote-access-firewall-fixture.py"), os.readlink("/proc/self/ns/net")]
        with subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, start_new_session=True) as process:
            try:
                output, errors = process.communicate(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
                raise
        self.assertEqual(process.returncode, 0, output + errors)


AIOHTTP = importlib.util.find_spec("aiohttp") is not None
if AIOHTTP:
    import asyncio
    from aiohttp import ClientPayloadError, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
    from aiohttp.test_utils import TestClient, TestServer
    from gateway import Gateway


@unittest.skipUnless(AIOHTTP, "install python3-aiohttp for gateway HTTP/WebSocket/SSE tests")
class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000.0
        self.config = configuration(self.directory.name)
        Path(self.config["users_file"]).write_text(json.dumps({"alice": password_hash("a sufficiently long password")}))
        self.sessions = Sessions(Path(self.directory.name) / "state.db", 30, 90, 125, lambda: self.now)
        self.gateway = Gateway(self.config, self.sessions)
        self.client = ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False,
                                    timeout=ClientTimeout(total=5))
        self.addAsyncCleanup(self.client.close)
        self.gateway.client = self.client
        self.upstream_requests = []
        self.local_lease = {"held": False}
        self.upstream = TestServer(web.Application())
        self.upstream.app.router.add_route("*", "/{path:.*}", self.upstream_handler)
        await self.upstream.start_server()
        self.addAsyncCleanup(self.upstream.close)
        self.clients = {}
        for service in self.config["services"]:
            self.config["services"][service]["upstream"] = str(self.upstream.make_url("/")).rstrip("/")
            client = TestClient(TestServer(self.gateway.application(service)))
            await client.start_server()
            self.clients[service] = client
            self.addAsyncCleanup(client.close)
        self.alice = self.sessions.login("alice")
        self.bob = self.sessions.login("bob")

    def headers(self, token=None, service="topology"):
        origin = self.gateway.origins()[service]
        return {"Host": origin.removeprefix("https://"), "Origin": origin,
                "Cookie": f"{self.gateway.cookie}={token or self.alice}"}

    async def upstream_handler(self, request):
        self.upstream_requests.append({"path": request.raw_path, "headers": dict(request.headers)})
        if request.path == "/api/demo/interactions":
            return web.json_response({"schema": "easymesh.room-demo.interactions.v1", "enabled": True, "lease": self.local_lease})
        if request.path == "/socket":
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            async for message in socket:
                if message.type == WSMsgType.TEXT:
                    await socket.send_str(message.data)
            return socket
        if request.path == "/events":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            try:
                while True:
                    await response.write(b"data: fresh\n\n")
                    await asyncio.sleep(.02)
            except (ConnectionError, asyncio.CancelledError):
                return response
        return web.json_response({"path": request.raw_path, "body": (await request.read()).decode()},
                                 headers={"Set-Cookie": "native=value; Path=/"})

    async def test_no_session_no_upstream_even_for_get(self):
        response = await self.clients["topology"].get("/api/topology", headers=self.headers())
        self.assertEqual(response.status, 423)
        self.assertEqual(self.upstream_requests, [])

    async def test_one_lock_covers_all_views_and_same_origin_apis(self):
        self.sessions.acquire(self.alice)
        for service, client in self.clients.items():
            response = await client.get("/api/test?sample=1", headers=self.headers(service=service))
            self.assertEqual(response.status, 200)
            self.assertEqual((await response.json())["path"], "/api/test?sample=1")
            denied = await client.post("/api/test", json={"act": True}, headers=self.headers(self.bob, service))
            self.assertEqual(denied.status, 423)
        self.assertEqual(len(self.upstream_requests), 3)

    async def test_busy_user_cannot_acquire_or_release(self):
        self.sessions.acquire(self.alice)
        for operation, status in (("acquire", 409), ("release", 403), ("activity", 403)):
            response = await self.clients["topology"].post("/_remote/" + operation, json={}, headers=self.headers(self.bob))
            self.assertEqual(response.status, status)

    async def test_login_cookie_secure_and_not_shared_with_backend(self):
        response = await self.clients["topology"].post("/_remote/login",
            json={"username": "alice", "password": "a sufficiently long password"}, headers=self.headers("invalid"))
        self.assertEqual(response.status, 200)
        cookie = response.cookies[self.gateway.cookie]
        self.assertTrue(cookie["secure"])
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["domain"], "")
        self.sessions.acquire(cookie.value)
        headers = self.headers(cookie.value)
        headers["Cookie"] += "; native=client"
        headers["Tailscale-User-Login"] = "forged@example.invalid"
        headers["X-Forwarded-Host"] = "forged.invalid"
        response = await self.clients["topology"].post("/api/test", json={"hello": "world"}, headers=headers)
        self.assertEqual(response.status, 200)
        upstream = self.upstream_requests[-1]["headers"]
        self.assertNotIn(self.gateway.cookie, upstream.get("Cookie", ""))
        self.assertIn("native=client", upstream["Cookie"])
        self.assertNotIn("Tailscale-User-Login", upstream)
        self.assertEqual(upstream["Host"], "lab.test.ts.net")
        self.assertEqual(upstream["Origin"], "https://lab.test.ts.net")

    async def test_wrong_host_origin_and_cross_site_are_denied(self):
        self.sessions.acquire(self.alice)
        for field, value in (("Host", "evil.invalid"), ("Origin", "https://evil.invalid"),
                             ("Sec-Fetch-Site", "cross-site")):
            headers = self.headers()
            headers[field] = value
            response = await self.clients["topology"].post("/api/test", json={}, headers=headers)
            self.assertEqual(response.status, 403)
        self.assertEqual(self.upstream_requests, [])

    async def test_native_local_operator_blocks_acquisition(self):
        self.local_lease = {"held": True, "owner": "local operator"}
        response = await self.clients["topology"].post("/_remote/acquire", json={}, headers=self.headers())
        self.assertEqual(response.status, 409)
        self.assertFalse(self.sessions.status(self.alice)["busy"])

    async def test_unknown_native_lease_refuses_acquisition(self):
        for invalid in ({}, {"held": "false"}, None):
            self.local_lease = invalid
            response = await self.clients["topology"].post("/_remote/acquire", json={}, headers=self.headers())
            self.assertEqual(response.status, 503)
            self.assertFalse(self.sessions.status(self.alice)["busy"])

    async def test_post_without_origin_refused(self):
        self.sessions.acquire(self.alice)
        headers = self.headers()
        headers.pop("Origin")
        response = await self.clients["topology"].post("/api/test", json={}, headers=headers)
        self.assertEqual(response.status, 403)
        self.assertEqual(self.upstream_requests, [])

    async def test_admin_maintenance_blocks_and_revokes(self):
        self.sessions.acquire(self.alice)
        self.sessions.set_maintenance(True)
        response = await self.clients["room"].get("/api/test", headers=self.headers(service="room"))
        self.assertEqual(response.status, 423)
        self.now += 126
        response = await self.clients["room"].post("/_remote/acquire", json={}, headers=self.headers(self.bob, "room"))
        self.assertEqual(response.status, 409)

    async def test_normal_acquisition_checks_native_lease(self):
        response = await self.clients["topology"].post("/_remote/acquire", json={}, headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["mine"])
        self.assertEqual(self.upstream_requests[0]["path"], "/api/demo/interactions")

    async def test_wrong_login_rate_limited(self):
        for index in range(6):
            response = await self.clients["topology"].post("/_remote/login",
                json={"username": "alice", "password": "wrong"}, headers=self.headers("invalid"))
            self.assertEqual(response.status, 401 if index < 5 else 429)

    async def test_anonymous_status_does_not_disclose_owner(self):
        self.sessions.acquire(self.alice)
        response = await self.clients["topology"].get("/_remote/status", headers=self.headers("invalid"))
        result = await response.json()
        self.assertFalse(result["authenticated"])
        self.assertNotIn("owner", result)

    async def test_sse_delivered_incrementally_and_closed_on_release(self):
        self.sessions.acquire(self.alice)
        response = await self.clients["room"].get("/events", headers=self.headers(service="room"))
        self.assertEqual(response.status, 200)
        self.assertEqual(await asyncio.wait_for(response.content.readline(), 1), b"data: fresh\n")
        self.sessions.release(self.alice)
        with self.assertRaises(ClientPayloadError):
            await asyncio.wait_for(response.read(), 2)

    async def test_websocket_is_bidirectional_and_closed_at_expiry(self):
        self.sessions.acquire(self.alice)
        socket = await self.clients["console"].ws_connect("/socket", headers=self.headers(service="console"))
        await socket.send_str("hello")
        self.assertEqual((await socket.receive(timeout=1)).data, "hello")
        self.now += 31
        message = await socket.receive(timeout=2)
        self.assertIn(message.type, (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR))
        self.assertFalse(self.sessions.status(self.alice)["mine"])

    async def test_unauthenticated_websocket_does_not_reach_backend(self):
        response = await self.clients["console"].get("/socket", headers={
            **self.headers(service="console"), "Upgrade": "websocket", "Connection": "Upgrade"})
        self.assertEqual(response.status, 423)
        self.assertEqual(self.upstream_requests, [])

    async def test_no_polling_renews_session(self):
        self.sessions.acquire(self.alice)
        self.now += 20
        for path in ("/_remote/status", "/api/test"):
            response = await self.clients["topology"].get(path, headers=self.headers())
            self.assertEqual(response.status, 200)
        self.assertEqual(self.sessions.status(self.alice)["idle_remaining"], 10)


if __name__ == "__main__":
    unittest.main()
