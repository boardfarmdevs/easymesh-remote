from __future__ import annotations

import asyncio
import json
import signal
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, DummyCookieJar, web
from aiohttp.test_utils import TestServer


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
from gateway import Gateway
from remote_state import Sessions, password_hash


async def main():
    with tempfile.TemporaryDirectory(prefix="easymesh-remote-fixture-") as directory:
        root = Path(directory)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=localhost", "-keyout", str(root / "key.pem"),
                        "-out", str(root / "cert.pem")], check=True, capture_output=True, timeout=10)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(root / "cert.pem", root / "key.pem")
        now = [1000.0]

        async def backend(request):
            if request.path == "/api/demo/interactions":
                return web.json_response({"schema": "easymesh.room-demo.interactions.v1", "enabled": True, "lease": {"held": False}})
            if request.path == "/api/echo":
                return web.json_response({"ok": True})
            if request.path == "/advance":
                now[0] += float((await request.json())["seconds"])
                return web.json_response({"now": now[0]})
            return web.Response(text="""<!doctype html><title>Lab fixture</title>
                <button id="probe">Exercise lab</button><p id="result">Ready</p>
                <script>document.getElementById('probe').onclick = () => {
                  document.getElementById('result').textContent = 'Clicked';
                }; setInterval(() => fetch('/api/echo').catch(() => {}), 200);</script>""",
                                content_type="text/html")

        application = web.Application()
        application.router.add_route("*", "/{path:.*}", backend)
        upstream = TestServer(application)
        await upstream.start_server()
        users = {username: password_hash("remote fixture password") for username in ("alice", "bob")}
        (root / "users.json").write_text(json.dumps(users))
        config = {"lab": "fixture", "hostname": "localhost", "state_directory": str(root),
                  "users_file": str(root / "users.json"), "idle_seconds": 30, "maximum_seconds": 90,
                  "handoff_seconds": 2, "services": {name: {"upstream": str(upstream.make_url("/")).rstrip("/"),
                  "public_port": 0} for name in ("topology", "console", "room")}}
        sessions = Sessions(root / "state.db", 30, 90, 2, lambda: now[0])
        gateway = Gateway(config, sessions)
        stopped = asyncio.Event()
        for signum in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(signum, stopped.set)
        runners = []
        async with ClientSession(cookie_jar=DummyCookieJar(), auto_decompress=False,
                                 timeout=ClientTimeout(total=5)) as client:
            gateway.client = client
            try:
                for service in config["services"]:
                    runner = web.AppRunner(gateway.application(service), access_log=None)
                    await runner.setup()
                    runners.append(runner)
                    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
                    await site.start()
                    config["services"][service]["public_port"] = site._server.sockets[0].getsockname()[1]
                print(json.dumps({"origins": gateway.origins(), "control": str(upstream.make_url("/advance"))}), flush=True)
                await stopped.wait()
            finally:
                for runner in runners:
                    await runner.cleanup()
                await upstream.close()


if __name__ == "__main__":
    asyncio.run(main())
