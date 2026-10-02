from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "gateway"))
from manage import firewall


def run(*arguments):
    return subprocess.run(arguments, check=True, text=True, capture_output=True, timeout=5)


def inside(process, *arguments):
    return run("nsenter", "--target", str(process.pid), "--net", *arguments)


def main():
    mapping = Path("/proc/self/uid_map").read_text().split()
    if len(mapping) != 3 or mapping[0] != "0" or mapping[2] != "1" or os.geteuid() != 0:
        raise SystemExit("Refusing: run only inside unshare --user --map-root-user --net.")
    if len(sys.argv) != 2 or os.readlink("/proc/self/ns/net") == sys.argv[1]:
        raise SystemExit("Refusing: a separate network namespace is required.")
    children = []
    with tempfile.TemporaryDirectory(prefix="remote-firewall-") as directory:
        try:
            for _name in ("visitor", "guest"):
                process = subprocess.Popen(["unshare", "--net", "sleep", "60"], stdin=subprocess.DEVNULL)
                children.append(process)
                for _attempt in range(100):
                    if os.readlink(f"/proc/{process.pid}/ns/net") != os.readlink("/proc/self/ns/net"):
                        break
                    time.sleep(.01)
                else:
                    raise RuntimeError("Child network namespace was not created.")
            visitor, guest = children
            run("ip", "link", "set", "lo", "up")
            for process, outer, peer, host_address, peer_address in (
                    (visitor, "public0", "visitor0", "192.0.2.1/24", "192.0.2.2/24"),
                    (guest, "vm0", "guest0", "10.77.0.1/24", "10.77.0.2/24")):
                run("ip", "link", "add", outer, "type", "veth", "peer", "name", peer)
                run("ip", "link", "set", peer, "netns", str(process.pid))
                run("ip", "address", "add", host_address, "dev", outer)
                run("ip", "link", "set", outer, "up")
                inside(process, "ip", "link", "set", "lo", "up")
                inside(process, "ip", "address", "add", peer_address, "dev", peer)
                inside(process, "ip", "link", "set", peer, "up")
                inside(process, "ip", "route", "add", "default", "via", host_address.split("/")[0])
            run("sysctl", "-w", "net.ipv4.ip_forward=1")
            run("sysctl", "-w", "net.ipv6.conf.all.forwarding=1")
            for process, interface, address in ((None, "public0", "fd42:1::1/64"),
                    (visitor, "visitor0", "fd42:1::2/64"), (None, "vm0", "fd42:2::1/64"),
                    (guest, "guest0", "fd42:2::2/64")):
                arguments = ("ip", "-6", "address", "add", address, "dev", interface, "nodad")
                inside(process, *arguments) if process else run(*arguments)
            inside(visitor, "ip", "-6", "route", "add", "default", "via", "fd42:1::1")
            inside(guest, "ip", "-6", "route", "add", "default", "via", "fd42:2::1")
            for port in (8891, 8892):
                children.append(subprocess.Popen(["nsenter", "--target", str(guest.pid), "--net",
                    sys.executable, "-m", "http.server", str(port), "--bind", "::"], cwd=directory,
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            nat = """table ip fixture_nat {
                chain pre { type nat hook prerouting priority -100; policy accept;
                    ip daddr 192.0.2.1 tcp dport 26342 dnat to 10.77.0.2:8891; }
                chain out { type nat hook output priority -100; policy accept;
                    ip daddr 192.0.2.1 tcp dport 26342 dnat to 10.77.0.2:8891; }
            }
            """
            subprocess.run(["nft", "--file", "-"], input=nat, text=True, check=True)
            time.sleep(.3)

            def probe(process, address, port, permitted):
                program = """import socket,sys
try:
 connection=socket.create_connection((sys.argv[1],int(sys.argv[2])),timeout=.5)
 connection.sendall(b'GET / HTTP/1.0\\r\\nHost: fixture\\r\\n\\r\\n')
 data=connection.recv(1024)
 connection.close()
 sys.exit(0 if b'200' in data else 2)
except OSError as error:
 print(error, file=sys.stderr)
 sys.exit(1)
"""
                arguments = [sys.executable, "-c", program, address, str(port)]
                if process:
                    arguments = ["nsenter", "--target", str(process.pid), "--net", *arguments]
                deadline = time.monotonic() + 3
                while True:
                    result = subprocess.run(arguments, check=False, timeout=3, capture_output=True, text=True)
                    if not permitted or result.returncode == 0 or time.monotonic() >= deadline:
                        break
                    time.sleep(.1)
                if (result.returncode == 0) != permitted:
                    raise AssertionError(f"{address}:{port} permitted={permitted}, exit={result.returncode}: {result.stderr}")

            config = {"lab": "firewall-fixture", "services": {"room": {
                "host_address": "192.0.2.1", "host_port": 26342, "guest_address": "10.77.0.2",
                "guest_addresses": ["10.77.0.2", "fd42:2::2"], "guest_port": 8891}}}
            probe(visitor, "192.0.2.1", 26342, True)
            probe(visitor, "10.77.0.2", 8891, True)
            probe(visitor, "fd42:2::2", 8891, True)
            firewall(config, True)
            probe(visitor, "192.0.2.1", 26342, False)
            probe(visitor, "10.77.0.2", 8891, False)
            probe(visitor, "fd42:2::2", 8891, False)
            probe(None, "192.0.2.1", 26342, True)
            probe(visitor, "10.77.0.2", 8892, True)
            firewall(config, True)
            probe(visitor, "192.0.2.1", 26342, False)
            firewall(config, False)
            probe(visitor, "192.0.2.1", 26342, True)
            print("PASS isolated firewall: NAT/direct IPv4/direct IPv6 denied, local and unrelated ports preserved, rollback works")
        finally:
            for process in reversed(children):
                process.terminate()
            for process in children:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
