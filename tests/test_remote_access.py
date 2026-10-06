from __future__ import annotations

import concurrent.futures
import copy
import importlib.util
import io
import ipaddress
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
import urllib.error
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
            result["Web"][endpoint] = {"Handlers": {"/": {"Proxy": remote_manage.expected_proxy(config, settings)}}}
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
                    patch.object(remote_manage, "check_gateway", side_effect=lambda *arguments: recorded.append(("ready",))), \
                    patch.object(remote_manage, "check_public", side_effect=lambda *arguments: recorded.append(("public",))):
                remote_manage.publish(config, "private")
            expose = [entry for entry in recorded if entry[0] == "tailscale"]
            self.assertEqual(len(expose), 3)
            self.assertTrue(all(entry[1] == "serve" and "--bg" in entry for entry in expose))
            self.assertLess(recorded.index(("firewall", True)), recorded.index(("ready",)))
            self.assertLess(recorded.index(("ready",)), recorded.index(expose[0]))
            self.assertEqual(recorded[-1], ("public",))

    def test_publication_rolled_back_when_a_public_url_reaches_something_else(self):
        # rev120: Apache on *:443 kept Serve from the Tailscale address's port 443.
        with tempfile.TemporaryDirectory() as directory:
            config = configuration(directory)
            Path(config["users_file"]).write_text('{"alice": "test-only-hash"}')
            with patch.object(remote_manage, "discover_services", return_value=config["services"]), \
                    patch.object(remote_manage, "read_command", side_effect=["{}", json.dumps(self.publication(config))]), \
                    patch.object(remote_manage, "command"), patch.object(remote_manage, "firewall"), \
                    patch.object(remote_manage, "check_gateway"), \
                    patch.object(remote_manage, "check_public", side_effect=ValueError("port 443 answered as another server")), \
                    patch.object(remote_manage.subprocess, "run") as rollback:
                with self.assertRaisesRegex(ValueError, "another server"):
                    remote_manage.publish(config, "private")
            self.assertEqual([entry.args[0][2] for entry in rollback.call_args_list],
                             ["--https=443", "--https=8443", "--https=10000"])

    def test_own_address_publishes_through_the_labs_node(self):
        with tempfile.TemporaryDirectory() as directory:
            config = configuration(directory)
            config.update(hostname="test-lab.tail.ts.net", tailscale_socket="/run/easymesh-remote-tailscale/test-lab/tailscaled.sock",
                          listen_address=remote_manage.listen_address("test-lab"))
            Path(config["users_file"]).write_text('{"alice": "test-only-hash"}')
            calls = []
            with patch.object(remote_manage, "discover_services", return_value=config["services"]), \
                    patch.object(remote_manage, "read_command", side_effect=lambda *arguments: calls.append(arguments) or (
                        "{}" if len(calls) == 1 else json.dumps(self.publication(config)))), \
                    patch.object(remote_manage, "command", side_effect=lambda *arguments: calls.append(arguments)), \
                    patch.object(remote_manage, "firewall"), patch.object(remote_manage, "check_gateway"), \
                    patch.object(remote_manage, "check_public"):
                remote_manage.publish(config, "private")
            tailscale = [call for call in calls if call[0] == "tailscale"]
            self.assertEqual(len(tailscale), 5)
            for call in tailscale:
                self.assertEqual(call[1:3], ("--socket", config["tailscale_socket"]))
            self.assertIn(f"http://{config['listen_address']}:41000", tailscale[1])

    def test_labs_node_reaches_only_the_gateways_on_loopback(self):
        unit = (REMOTE / "easymesh-remote-tailscale@.service").read_text()
        self.assertIn("--tun=userspace-networking", unit)
        self.assertIn("DynamicUser=yes", unit)
        self.assertIn("IPAddressDeny=localhost", unit)
        allowed = [ipaddress.ip_network(entry) for line in unit.splitlines() if line.startswith("IPAddressAllow=")
                   for entry in line.removeprefix("IPAddressAllow=").split()]
        self.assertFalse(any(ipaddress.ip_address("127.0.0.1") in network for network in allowed))
        for lab in ("rdk-emosa", "rdk", "prpl", "emosa-osl", "easymesh-lab"):
            address = ipaddress.ip_address(remote_manage.listen_address(lab))
            self.assertTrue(any(address in network for network in allowed), lab)
            self.assertTrue(address.is_loopback)
        self.assertEqual(remote_manage.listen_address("rdk-emosa"), remote_manage.listen_address("rdk-emosa"))

    def own_address(self, states, published="{}"):
        with tempfile.TemporaryDirectory() as directory:
            config = configuration(directory)
            calls, saved = [], []
            statuses = iter(states)
            def read(*arguments):
                calls.append(arguments)
                return published if "serve" in arguments else json.dumps(next(statuses))
            with patch.object(remote_manage, "read_command", side_effect=read), \
                    patch.object(remote_manage, "command", side_effect=lambda *arguments: calls.append(arguments)), \
                    patch.object(remote_manage.subprocess, "run", side_effect=lambda arguments, **kwargs: calls.append(tuple(arguments))), \
                    patch.object(remote_manage, "save_json", side_effect=lambda path, value, owner: saved.append(copy.deepcopy(value))), \
                    patch.object(remote_manage.pwd, "getpwnam"):
                try:
                    remote_manage.own_address(config, "test-lab")
                finally:
                    self.calls, self.saved = calls, saved

    def test_own_address_logs_the_labs_node_in_and_moves_the_gateway(self):
        self.own_address([{"BackendState": "NeedsLogin", "Self": {"DNSName": ""}},
                          {"BackendState": "Running", "Self": {"DNSName": "test-lab.tail.ts.net."}}])
        self.assertIn(("systemctl", "enable", "--now", "easymesh-remote-tailscale@test-lab.service"), self.calls)
        login = next(call for call in self.calls if "up" in call)
        self.assertEqual(login[:3], ("tailscale", "--socket", "/run/easymesh-remote-tailscale/test-lab/tailscaled.sock"))
        self.assertIn("--hostname=test-lab", login)
        self.assertEqual(self.saved[-1]["hostname"], "test-lab.tail.ts.net")
        self.assertEqual(self.saved[-1]["listen_address"], remote_manage.listen_address("test-lab"))
        self.assertEqual(self.calls[-1], ("systemctl", "try-restart", "easymesh-remote@test-lab.service"))

    def test_own_address_refuses_a_taken_name_and_a_published_lab(self):
        with self.assertRaisesRegex(ValueError, "taken by another device"):
            self.own_address([{"BackendState": "Running", "Self": {"DNSName": "test-lab-1.tail.ts.net."}}])
        self.assertEqual(self.saved, [])
        published = {"Web": {"lab.test.ts.net:443": {"Handlers": {}}}}
        with self.assertRaisesRegex(ValueError, "Unpublish first"):
            self.own_address([], published=json.dumps(published))
        self.assertFalse(any(call[:2] == ("systemctl", "enable") for call in self.calls))

    def test_public_check_names_who_answers(self):
        config = configuration("/tmp")
        gateway = {name: {"schema": "easymesh.remote.session.v1", "lab": "test-lab", "service": name}
                   for name in config["services"]}

        def opener(answers):
            def open_url(url, timeout):
                answer = answers(url)
                if isinstance(answer, Exception):
                    raise answer
                return io.BytesIO(json.dumps(answer).encode())
            return type("Opener", (), {"open": staticmethod(open_url)})()

        def by_port(url):
            port = url.split("/")[2].partition(":")[2] or "443"
            return next(name for name, settings in config["services"].items() if str(settings["public_port"]) == port)

        with patch.object(remote_manage.urllib.request, "build_opener", return_value=opener(lambda url: gateway[by_port(url)])):
            remote_manage.check_public(config, wait=0)
        expired = urllib.error.URLError("certificate verify failed: certificate has expired")
        for answers in (lambda url: expired if ":" not in url.split("/")[2] else gateway[by_port(url)],
                        lambda url: {"other": "server"}):
            with self.subTest(), patch.object(remote_manage.urllib.request, "build_opener", return_value=opener(answers)):
                with self.assertRaisesRegex(ValueError, r"https://lab.test.ts.net/_remote/status does not reach this gateway.*'sport = :443'"):
                    remote_manage.check_public(config, wait=0)

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
        # The page and each tile renew the reservation on genuine input only, the tile in its own view.
        for name in ("app.js", "tile.js"):
            source = (REMOTE / "web" / name).read_text()
            with self.subTest(name=name):
                self.assertIn("!event.isTrusted", source)
                self.assertIn("document.visibilityState !== 'visible'", source)
                self.assertIn("!document.hasFocus()", source)
                self.assertIn("now - lastActivity < 15000", source)
                self.assertNotIn("setInterval(activity", source)
        self.assertIn("setInterval(refresh, 5000)", (REMOTE / "web/app.js").read_text())
        self.assertIn("view.contentWindow.document", (REMOTE / "web/tile.js").read_text())

    def test_users_file_takes_both_entry_forms(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.json"
            path.write_text(json.dumps({"alice": "scrypt:aa:bb", "carol": {"password": "scrypt:cc:dd", "role": "admin"}}))
            self.assertEqual(remote_manage.load_users(path), {
                "alice": {"password": "scrypt:aa:bb", "role": "operator"},
                "carol": {"password": "scrypt:cc:dd", "role": "admin"}})

    def test_lab_card_from_configuration_and_vm(self):
        instance = {"metadata": {"created_at": "2026-10-02T09:14:00Z"}}
        with patch.object(remote_manage, "read_command", return_value=json.dumps(instance)):
            card = remote_manage.lab_card("rdk-emosa", "rdk-emosa-1002")
            self.assertEqual((card["title"], card["vm"], card["built"]), ("RDK lab + EMOSA", "rdk-emosa-1002", "2026-10-02"))
            self.assertIn("OpenSync pods", card["summary"])
            self.assertEqual(remote_manage.lab_card("prpl-1002", "prpl-1002")["title"], "prplMesh lab")
            self.assertEqual(remote_manage.lab_card("demo-a", "demo-a", "Demo", "")["title"], "Demo")

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
        self.lease_parts = 0
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
            snapshot = {"schema": "easymesh.room-demo.interactions.v1", "enabled": True, "lease": self.local_lease}
            if not self.lease_parts:
                return web.json_response(snapshot)
            body = json.dumps({**snapshot, "roles": ["sta_static_01"] * 2000}).encode()
            response = web.StreamResponse(headers={"Content-Type": "application/json"})
            response.content_length = len(body)
            await response.prepare(request)
            step = len(body) // self.lease_parts + 1
            for start in range(0, len(body), step):
                await response.write(body[start:start + step])
                await asyncio.sleep(.01)
            await response.write_eof()
            return response
        if request.path == "/framed":
            return web.Response(text="<!doctype html>", content_type="text/html",
                                headers={"Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'"})
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
        # A __Host- cookie must not name a domain (older aiohttp clients fill cookie["domain"] in themselves).
        self.assertNotIn("domain=", response.headers["Set-Cookie"].lower())
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

    async def test_logout_ends_reservation_and_clears_cookie(self):
        self.sessions.acquire(self.alice)
        response = await self.clients["topology"].post("/_remote/logout", json={}, headers=self.headers())
        self.assertEqual(response.status, 200)
        cleared = response.headers["Set-Cookie"]
        self.assertTrue(cleared.startswith(self.gateway.cookie + '=""') or cleared.startswith(self.gateway.cookie + "=;"))
        for attribute in ("max-age=0", "secure", "httponly", "samesite=strict", "path=/"):
            self.assertIn(attribute, cleared.lower())
        self.assertFalse(self.sessions.status(self.alice)["authenticated"])
        self.assertFalse(self.sessions.status()["busy"])

    async def test_views_framed_by_the_labs_own_origins_only(self):
        # Console NG sends frame-ancestors 'none', which would keep it out of the lab's page.
        self.sessions.acquire(self.alice)
        response = await self.clients["console"].get("/framed", headers=self.headers(service="console"))
        self.assertEqual(response.status, 200)
        self.assertNotIn("X-Frame-Options", response.headers)
        self.assertEqual(response.headers.getall("Content-Security-Policy"), ["default-src 'self'",
            "frame-ancestors https://lab.test.ts.net https://lab.test.ts.net:8443 https://lab.test.ts.net:10000"])

    async def test_page_frames_only_tiles_and_tiles_only_their_view(self):
        page = await self.clients["room"].get("/_remote/", headers=self.headers(service="room"))
        self.assertEqual(page.status, 200)
        self.assertEqual(page.headers["X-Frame-Options"], "DENY")
        policy = page.headers["Content-Security-Policy"]
        self.assertIn("frame-src https://lab.test.ts.net/_remote/tile https://lab.test.ts.net:8443/_remote/tile "
                      "https://lab.test.ts.net:10000/_remote/tile;", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertIn("script-src 'self';", policy)
        tile = await self.clients["room"].get("/_remote/tile", headers=self.headers(service="room"))
        self.assertEqual(tile.status, 200)
        self.assertNotIn("X-Frame-Options", tile.headers)
        policy = tile.headers["Content-Security-Policy"]
        self.assertIn("frame-src 'self';", policy)
        self.assertIn("frame-ancestors https://lab.test.ts.net https://lab.test.ts.net:8443 https://lab.test.ts.net:10000", policy)
        for name in ("app.js", "app.css", "tile.js", "icon.svg"):
            response = await self.clients["room"].get("/_remote/" + name, headers=self.headers(service="room"))
            self.assertEqual(response.status, 200, name)
        for name in ("index.html", "tile.html", "portal.js", "web/app.js", "app.js/"):
            response = await self.clients["room"].get("/_remote/" + name, headers=self.headers(service="room"))
            self.assertEqual(response.status, 404, name)
        self.assertEqual(self.upstream_requests, [])

    async def test_status_tells_the_card_to_everyone_and_the_rest_to_accounts(self):
        self.config["card"] = {"title": "RDK lab + EMOSA", "summary": "Pods.", "vm": "rdk-emosa-1002",
                               "host": "rev120", "built": "2026-10-02"}
        anonymous = await (await self.clients["topology"].get("/_remote/status", headers=self.headers("invalid"))).json()
        self.assertEqual(anonymous["card"], {"title": "RDK lab + EMOSA", "summary": "Pods."})
        self.assertEqual(anonymous["views"]["console"]["origin"], "https://lab.test.ts.net:8443")
        self.assertEqual(anonymous["views"]["room"]["title"], "Room")
        self.assertEqual(anonymous["rules"], {"idle_seconds": 30, "maximum_seconds": 90, "handoff_seconds": 125})
        self.assertNotIn("role", anonymous)
        signed_in = await (await self.clients["topology"].get("/_remote/status", headers=self.headers())).json()
        self.assertEqual(signed_in["card"]["vm"], "rdk-emosa-1002")
        self.assertEqual(signed_in["role"], "operator")
        self.assertNotIn("signed_in", signed_in)

    async def test_only_an_administrator_releases_or_maintains_from_the_page(self):
        users = json.loads(Path(self.config["users_file"]).read_text())
        users["carol"] = {"password": password_hash("another long password"), "role": "admin"}
        Path(self.config["users_file"]).write_text(json.dumps(users))
        carol = (await (await self.clients["topology"].post("/_remote/login", json={
            "username": "carol", "password": "another long password"}, headers=self.headers("invalid"))).json())
        self.assertEqual(carol["role"], "admin")
        carol_token = self.sessions.login("carol")
        self.sessions.acquire(self.alice)
        denied = await self.clients["topology"].post("/_remote/admin", json={"action": "release"}, headers=self.headers(self.bob))
        self.assertEqual(denied.status, 403)
        self.assertTrue(self.sessions.status()["busy"])
        status = await (await self.clients["topology"].get("/_remote/status", headers=self.headers(carol_token))).json()
        self.assertEqual([entry["username"] for entry in status["signed_in"]], ["alice", "bob", "carol"])
        released = await self.clients["topology"].post("/_remote/admin", json={"action": "release"}, headers=self.headers(carol_token))
        self.assertEqual(released.status, 200)
        self.assertFalse(self.sessions.status(self.alice)["mine"])
        self.assertEqual(self.sessions.status()["handoff_remaining"], 125)
        response = await self.clients["topology"].post("/_remote/admin", json={"action": "maintenance-on"}, headers=self.headers(carol_token))
        self.assertTrue((await response.json())["maintenance"])
        unknown = await self.clients["topology"].post("/_remote/admin", json={"action": "reboot"}, headers=self.headers(carol_token))
        self.assertEqual(unknown.status, 400)
        users["carol"]["role"] = "operator"
        Path(self.config["users_file"]).write_text(json.dumps(users))
        demoted = await self.clients["topology"].post("/_remote/admin", json={"action": "maintenance-off"}, headers=self.headers(carol_token))
        self.assertEqual(demoted.status, 403)
        self.assertTrue(self.sessions.status()["maintenance"])

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

    async def test_native_lease_read_whole_when_it_arrives_in_parts(self):
        # The live room's snapshot is about 27 KB and arrives in several reads.
        self.lease_parts = 8
        response = await self.clients["topology"].post("/_remote/acquire", json={}, headers=self.headers())
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["mine"])

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

    async def test_session_ending_while_waiting_for_the_gateway_is_423(self):
        # aiohttp has no HTTPLocked: this answered 500, and a stream's end logged a traceback (rev120, 6 October).
        self.sessions.acquire(self.alice)
        for _ in range(64):
            await self.gateway.requests.acquire()
        try:
            request = asyncio.create_task(self.clients["room"].get("/api/test", headers=self.headers(service="room")))
            await asyncio.sleep(.1)
            self.sessions.release(self.alice)
        finally:
            for _ in range(64):
                self.gateway.requests.release()
        response = await asyncio.wait_for(request, 2)
        self.assertEqual(response.status, 423)
        self.assertEqual(self.upstream_requests, [])

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
