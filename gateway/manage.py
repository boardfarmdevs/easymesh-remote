#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import hashlib
import ipaddress
import json
import os
import pwd
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from remote_state import Sessions, password_hash


CONFIG_ROOT = Path("/etc/easymesh-remote")
STATE_ROOT = Path("/var/lib/easymesh-remote")
DEVICES = {"topology": "easymesh-webui", "console": "wmediumd-console", "room": "room-demo-viewer"}
PUBLIC_PORTS = {"topology": 443, "console": 8443, "room": 10000}


def command(*arguments, **kwargs):
    return subprocess.run(arguments, check=True, text=True, stdin=subprocess.DEVNULL, timeout=60, **kwargs)


def read_command(*arguments):
    return command(*arguments, capture_output=True).stdout


def valid_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", value):
        raise ValueError("Names must start with a lowercase letter and contain at most 32 letters, digits or hyphens.")
    return value


def save_json(path, value, owner=None):
    descriptor, temporary = tempfile.mkstemp(prefix=".remote-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o640)
        if owner:
            os.chown(temporary, 0, owner.pw_gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def parse_endpoint(value):
    protocol, address, port_text = value.rsplit(":", 2)
    if protocol != "tcp":
        raise ValueError("Only single-port IPv4 TCP LXD proxies are supported.")
    parsed = ipaddress.IPv4Address(address)
    port = int(port_text)
    if not 1 <= port <= 65535 or parsed.is_unspecified or parsed.is_multicast:
        raise ValueError("The LXD proxy needs a specific IPv4 address and one valid TCP port.")
    return str(parsed), port


def discover_services(vm, local_base):
    data = json.loads(read_command("lxc", "query", "/1.0/instances/" + vm))
    data = data.get("metadata", data)
    devices = data["expanded_devices"]
    state = json.loads(read_command("lxc", "query", "/1.0/instances/" + vm + "/state"))
    networks = state.get("metadata", state)["network"]
    services = {}
    for offset, (name, device_name) in enumerate(DEVICES.items()):
        device = devices.get(device_name, {})
        if device.get("type") != "proxy" or device.get("nat") != "true":
            raise ValueError(f"{vm} must have the NAT proxy {device_name}; use the named RDK VM setup first.")
        host, host_port = parse_endpoint(device["listen"])
        guest, guest_port = parse_endpoint(device["connect"])
        addresses = set()
        for interface in networks.values():
            records = interface.get("addresses", [])
            if any(record.get("address") == guest for record in records):
                addresses.update(str(ipaddress.ip_address(record["address"])) for record in records)
        if guest not in addresses:
            raise ValueError(f"Cannot verify {guest} and its companion IPv6 addresses; start the VM and its LXD agent first.")
        services[name] = {"upstream": f"http://{host}:{host_port}", "host_address": host,
                          "host_port": host_port, "guest_address": guest, "guest_port": guest_port,
                          "guest_addresses": sorted(addresses),
                          "local_port": local_base + offset, "public_port": PUBLIC_PORTS[name]}
    return services


def configure(args):
    config_path = CONFIG_ROOT / (args.lab + ".json")
    if config_path.exists():
        raise ValueError(f"Already configured: {config_path}. Unpublish and stop the gateway before editing that file.")
    hostname = args.hostname
    if not hostname:
        status = json.loads(read_command("tailscale", "status", "--json"))
        if status.get("BackendState") != "Running":
            raise ValueError("Run sudo tailscale up first.")
        hostname = status["Self"]["DNSName"].rstrip(".")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.ts\.net", hostname):
        raise ValueError("A lowercase Tailscale *.ts.net hostname is required.")
    if not 30 <= args.idle_seconds <= args.maximum_seconds <= 8 * 3600:
        raise ValueError("Require 30 <= idle-seconds <= maximum-seconds <= 28800.")
    if not 125 <= args.handoff_seconds <= 600:
        raise ValueError("handoff-seconds must be 125..600, exceeding the room's maximum 120-second lease.")
    local_base = args.local_port_base or 40000 + int(hashlib.sha256(args.lab.encode()).hexdigest()[:6], 16) % 1000 * 3
    if not 1024 <= local_base <= 65533:
        raise ValueError("local-port-base must leave three nonprivileged TCP ports.")
    services = discover_services(valid_name(args.vm or args.lab), local_base)
    if len({settings["host_port"] for settings in services.values()}) != 3:
        raise ValueError("Each service needs its own host port.")
    for other in CONFIG_ROOT.glob("*.json"):
        existing = json.loads(other.read_text())
        occupied = {settings["local_port"] for settings in existing.get("services", {}).values()}
        if occupied.intersection({local_base, local_base + 1, local_base + 2}):
            raise ValueError("Gateway listener conflict; choose --local-port-base.")
    account = pwd.getpwnam("easymesh-remote")
    directory = STATE_ROOT / args.lab
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(directory, account.pw_uid, account.pw_gid)
    users_path = CONFIG_ROOT / (args.lab + ".users.json")
    if not users_path.exists():
        save_json(users_path, {}, account)
    config = {"lab": args.lab, "vm": args.vm or args.lab, "hostname": hostname,
              "services": services, "idle_seconds": args.idle_seconds,
              "maximum_seconds": args.maximum_seconds, "handoff_seconds": args.handoff_seconds,
              "users_file": str(users_path), "state_directory": str(directory)}
    save_json(config_path, config, account)
    Sessions(directory / "sessions.sqlite3")
    os.chown(directory / "sessions.sqlite3", account.pw_uid, account.pw_gid)
    print(json.dumps(config, indent=2))
    print("Configured only; add-user, then publish. No service or firewall was changed.")


def firewall_table(config):
    return "em_remote_" + hashlib.sha256(config["lab"].encode()).hexdigest()[:12]


def firewall_rules(config, replace=False):
    table = firewall_table(config)
    lines = [f"delete table inet {table}"] if replace else []
    lines.extend([f"table inet {table} {{", " chain ingress {",
                  "  type filter hook prerouting priority -110; policy accept;"])
    for settings in config["services"].values():
        host = str(ipaddress.IPv4Address(settings["host_address"]))
        host_port = int(settings["host_port"])
        guest_port = int(settings["guest_port"])
        lines.append(f'  iifname != "lo" ip daddr {host} tcp dport {host_port} counter drop')
        for raw_address in settings["guest_addresses"]:
            guest = ipaddress.ip_address(raw_address)
            family = "ip6" if guest.version == 6 else "ip"
            lines.append(f'  iifname != "lo" {family} daddr {guest} tcp dport {guest_port} counter drop')
    lines.extend([" }", "}"])
    return "\n".join(lines) + "\n"


def firewall(config, enabled):
    table = firewall_table(config)
    exists = subprocess.run(["nft", "list", "table", "inet", table],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
    if enabled:
        rules = firewall_rules(config, replace=exists)
        subprocess.run(["nft", "--check", "--file", "-"], input=rules, text=True, check=True)
        subprocess.run(["nft", "--file", "-"], input=rules, text=True, check=True)
    elif exists:
        command("nft", "delete", "table", "inet", table)


def expected_proxy(settings):
    return "http://127.0.0.1:" + str(settings["local_port"])


def check_publication(config, current):
    for settings in config["services"].values():
        port = str(settings["public_port"])
        listener = current.get("TCP", {}).get(port)
        endpoint = config["hostname"] + ":" + port
        website = current.get("Web", {}).get(endpoint)
        expected = {"Handlers": {"/": {"Proxy": expected_proxy(settings)}}}
        related = [name for name in current.get("Web", {}) if name.endswith(":" + port)]
        if listener or website or related:
            if listener != {"HTTPS": True} or website != expected or related != [endpoint]:
                raise ValueError(f"Tailscale port {port} already serves something else; refusing to overwrite it.")


def check_gateway(config):
    for name, settings in config["services"].items():
        endpoint = config["hostname"] + ("" if settings["public_port"] == 443 else ":" + str(settings["public_port"]))
        request = urllib.request.Request(expected_proxy(settings) + "/_remote/status", headers={"Host": endpoint})
        deadline = time.monotonic() + 10
        while True:
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(request, timeout=1) as response:
                    status = json.load(response)
                if status.get("schema") != "easymesh.remote.session.v1" or status.get("lab") != config["lab"] or status.get("service") != name:
                    raise ValueError(f"Port {settings['local_port']} is not the expected gateway; refusing publication.")
                break
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise ValueError(f"Gateway {name} listener is not ready; inspect its systemd journal.")
                time.sleep(.2)


def publish(config, mode, confirmed=False):
    if mode == "public" and not confirmed:
        raise ValueError("Public access requires --confirm-public. Login still remains mandatory.")
    if not json.loads(Path(config["users_file"]).read_text()):
        raise ValueError("Add at least one user before publication.")
    discovered = discover_services(config["vm"], config["services"]["topology"]["local_port"])
    if discovered != config["services"]:
        raise ValueError("VM port forwards changed. Unpublish, stop the gateway and update its configuration first.")
    current = json.loads(read_command("tailscale", "serve", "status", "--json"))
    check_publication(config, current)
    command("systemctl", "enable", "--now", f"easymesh-remote-firewall@{config['lab']}.service")
    firewall(config, True)
    command("systemctl", "enable", "--now", f"easymesh-remote@{config['lab']}.service")
    command("systemctl", "is-active", "--quiet", f"easymesh-remote@{config['lab']}.service")
    check_gateway(config)
    installed = []
    verb = "funnel" if mode == "public" else "serve"
    try:
        for settings in config["services"].values():
            port = str(settings["public_port"])
            installed.append(port)
            command("tailscale", verb, "--bg", "--https=" + port, expected_proxy(settings))
        final = json.loads(read_command("tailscale", "serve", "status", "--json"))
        check_publication(config, final)
        for settings in config["services"].values():
            port = str(settings["public_port"])
            endpoint = config["hostname"] + ":" + port
            if port not in final.get("TCP", {}) or bool(final.get("AllowFunnel", {}).get(endpoint)) != (mode == "public"):
                raise ValueError("Tailscale publication mode did not match the requested mode.")
    except (subprocess.SubprocessError, ValueError):
        for port in installed:
            subprocess.run(["tailscale", verb, "--https=" + port, "off"], check=False)
        raise
    print(f"Published {mode}; login and reservation required on every application/API/stream.")
    show_urls(config)


def unpublish(config):
    current = json.loads(read_command("tailscale", "serve", "status", "--json"))
    check_publication(config, current)
    for settings in config["services"].values():
        port = str(settings["public_port"])
        if port in current.get("TCP", {}):
            endpoint = config["hostname"] + ":" + port
            verb = "funnel" if current.get("AllowFunnel", {}).get(endpoint) else "serve"
            command("tailscale", verb, "--https=" + port, "off")
    print("Unpublished only this gateway. Direct-port protection remains enabled.")


def show_urls(config):
    for name, settings in config["services"].items():
        port = settings["public_port"]
        print(f"{name}: https://{config['hostname']}{'' if port == 443 else ':' + str(port)}/_remote/")


def main():
    parser = argparse.ArgumentParser(description="Configure and publish an optional, exclusive-session RDK lab gateway.")
    parser.add_argument("--lab", default=os.environ.get("EASYMESH_LXD_NAME", "easymesh"))
    commands = parser.add_subparsers(dest="operation", required=True)
    setup = commands.add_parser("configure", help="discover existing VM port forwards; do not expose anything")
    setup.add_argument("--vm")
    setup.add_argument("--hostname")
    setup.add_argument("--local-port-base", type=int)
    setup.add_argument("--idle-seconds", type=int, default=600)
    setup.add_argument("--maximum-seconds", type=int, default=3600)
    setup.add_argument("--handoff-seconds", type=int, default=125)
    for operation in ("add-user", "remove-user"):
        commands.add_parser(operation).add_argument("username")
    publisher = commands.add_parser("publish")
    publisher.add_argument("--mode", choices=("private", "public"), default="private")
    publisher.add_argument("--confirm-public", action="store_true")
    for operation in ("status", "release", "unpublish", "firewall-on", "firewall-off", "maintenance-on", "maintenance-off"):
        commands.add_parser(operation)
    args = parser.parse_args()
    valid_name(args.lab)
    if os.geteuid() != 0:
        parser.error("run with sudo on the outer LXD host")
    os.umask(0o077)
    if args.operation == "configure":
        configure(args)
        return
    config = json.loads((CONFIG_ROOT / (args.lab + ".json")).read_text())
    sessions = Sessions(Path(config["state_directory"]) / "sessions.sqlite3", config["idle_seconds"],
                        config["maximum_seconds"], config["handoff_seconds"])
    if args.operation in {"add-user", "remove-user"}:
        username = valid_name(args.username)
        path = Path(config["users_file"])
        users = json.loads(path.read_text())
        if args.operation == "add-user":
            password = getpass.getpass("Password (at least 16 characters): ")
            if len(password) < 16 or len(password) > 512 or password != getpass.getpass("Repeat password: "):
                raise ValueError("Passwords must match and contain 16..512 characters.")
            users[username] = password_hash(password)
        else:
            users.pop(username, None)
        save_json(path, users, pwd.getpwnam("easymesh-remote"))
        sessions.revoke_user(username)
        print("Credentials updated; existing sessions for this user were revoked.")
    elif args.operation == "publish":
        publish(config, args.mode, args.confirm_public)
    elif args.operation == "unpublish":
        unpublish(config)
    elif args.operation.startswith("firewall-"):
        firewall(config, args.operation == "firewall-on")
    elif args.operation == "release":
        sessions.release(force=True)
        print("Reservation released; active streams close within one second. Handoff delay still applies.")
    elif args.operation.startswith("maintenance-"):
        sessions.set_maintenance(args.operation == "maintenance-on")
        print("Maintenance updated. Stop local tests and release their native room lease before reopening remote access.")
    elif args.operation == "status":
        print(json.dumps(sessions.status(), indent=2))
        show_urls(config)
        command("systemctl", "--no-pager", "status", f"easymesh-remote@{args.lab}.service",
                f"easymesh-remote-firewall@{args.lab}.service")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"remote access: {error}", file=sys.stderr)
        sys.exit(1)
