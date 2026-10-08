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
import socket
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
# The lab card's defaults, by configuration (the umbrella's lab configurations).
CONFIGURATIONS = {
    "rdk": ("RDK EasyMesh lab", "Optimizer development on RDK: the gateway and controller, four Wi-Fi extenders "
            "and a wired one, 100 room clients, the RF medium and the interactive room."),
    "prpl": ("prplMesh lab", "The same optimizer lab on native prplMesh, with a wired Agent."),
    "emosa-osl": ("OpenSync + EMOSA, prplMesh controller", "Adapter development against a prplMesh controller: "
                  "several pods, the fault workload, the EasyMesh wireless backhaul."),
    "rdk-emosa": ("RDK lab + EMOSA", "OpenSync pods as EasyMesh agents next to the RDK lab's native agents, "
                  "in the standard rooms."),
    "easymesh-lab": ("Physical protocol lab", "The EasyMesh protocol on certified hardware: a controller and "
                     "teaching panel, two extenders, real clients."),
}
ROLES = ("operator", "admin")


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


def lab_card(lab, vm, title=None, summary=None):
    # A configuration's VMs are named after it and their build date (rdk-emosa-1005).
    configuration = re.sub(r"-\d{4}[a-z]?$", "", lab)
    default_title, default_summary = CONFIGURATIONS.get(configuration, (lab, ""))
    data = json.loads(read_command("lxc", "query", "/1.0/instances/" + vm))
    created = data.get("metadata", data).get("created_at", "")
    return {"title": title or default_title, "summary": summary if summary is not None else default_summary,
            "vm": vm, "host": os.uname().nodename, "built": created[:10]}


def load_users(path):
    users = json.loads(Path(path).read_text())
    # An entry is a password hash (an operator, as first written) or {"password": ..., "role": ...}.
    return {name: entry if isinstance(entry, dict) else {"password": entry, "role": "operator"}
            for name, entry in users.items()}


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
              "card": lab_card(args.lab, args.vm or args.lab, args.title, args.summary),
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


def tailscale(config):
    """The tailscale command for the lab's node: its own (address) or the host's."""
    return ["tailscale"] + (["--socket", config["tailscale_socket"]] if config.get("tailscale_socket") else [])


def expected_proxy(config, settings):
    return f"http://{config.get('listen_address', '127.0.0.1')}:{settings['local_port']}"


def check_publication(config, current):
    for settings in config["services"].values():
        port = str(settings["public_port"])
        listener = current.get("TCP", {}).get(port)
        endpoint = config["hostname"] + ":" + port
        website = current.get("Web", {}).get(endpoint)
        expected = {"Handlers": {"/": {"Proxy": expected_proxy(config, settings)}}}
        related = [name for name in current.get("Web", {}) if name.endswith(":" + port)]
        if listener or website or related:
            if listener != {"HTTPS": True} or website != expected or related != [endpoint]:
                raise ValueError(f"Tailscale port {port} already serves something else; refusing to overwrite it.")


def check_gateway(config):
    for name, settings in config["services"].items():
        endpoint = config["hostname"] + ("" if settings["public_port"] == 443 else ":" + str(settings["public_port"]))
        request = urllib.request.Request(expected_proxy(config, settings) + "/_remote/status", headers={"Host": endpoint})
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


def public_url(config, settings):
    port = settings["public_port"]
    return f"https://{config['hostname']}{'' if port == 443 else ':' + str(port)}"


def check_public(config, wait=60):
    # Serve's configuration can be right while another program holds the port on the Tailscale
    # address (a web server on *:443): only a request to the public URL shows who answers.
    # Waiting covers the certificate Tailscale fetches on first use.
    if config.get("tailscale_socket"):
        try:
            socket.getaddrinfo(config["hostname"], 443)
        except socket.gaierror:
            # A lab's own device: its ports exist only in its userspace tailscaled, so no program of
            # the host can hold them; a host off the tailnet cannot even resolve the name (rev150).
            print(f"Not requested from this host, which cannot resolve {config['hostname']} (it is not on the "
                  "tailnet); the lab's own device holds its ports alone. Open it from a tailnet device.")
            return
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for name, settings in config["services"].items():
        url = public_url(config, settings) + "/_remote/status"
        deadline = time.monotonic() + wait
        while True:
            try:
                with opener.open(url, timeout=5) as response:
                    status = json.load(response)
                if status.get("schema") == "easymesh.remote.session.v1" and status.get("lab") == config["lab"] \
                        and status.get("service") == name:
                    break
                problem = "another service answered"
            except (urllib.error.URLError, OSError, ValueError) as error:
                problem = str(getattr(error, "reason", error))
            if time.monotonic() >= deadline:
                port = settings["public_port"]
                raise ValueError(f"{url} does not reach this gateway ({problem}). Another program may listen on "
                                 f"port {port} of the Tailscale address: sudo ss -ltnp 'sport = :{port}'")
            time.sleep(2)


def publish(config, mode, confirmed=False, close_lan=False, keep_lan_open=False):
    if mode == "public" and not confirmed:
        raise ValueError("Public access requires --confirm-public. Login still remains mandatory.")
    if keep_lan_open and (mode != "public" or close_lan):
        raise ValueError("--keep-lan-open is for a public lab, and not with --close-lan.")
    if not json.loads(Path(config["users_file"]).read_text()):
        raise ValueError("Add at least one user before publication.")
    discovered = discover_services(config["vm"], config["services"]["topology"]["local_port"])
    if discovered != config["services"]:
        raise ValueError(f"The VM's port forwards changed (a rebuild?): retarget --vm {config['vm']}, or the new VM.")
    current = json.loads(read_command(*tailscale(config), "serve", "status", "--json"))
    check_publication(config, current)
    # The lab network is trusted: the lab's ports close to it only for a public lab, or on request.
    firewall_unit = f"easymesh-remote-firewall@{config['lab']}.service"
    if (mode == "public" and not keep_lan_open) or close_lan:
        command("systemctl", "enable", "--now", firewall_unit)
        firewall(config, True)
    else:
        command("systemctl", "disable", "--now", firewall_unit)
        firewall(config, False)
    command("systemctl", "enable", "--now", f"easymesh-remote@{config['lab']}.service")
    command("systemctl", "is-active", "--quiet", f"easymesh-remote@{config['lab']}.service")
    check_gateway(config)
    installed = []
    verb = "funnel" if mode == "public" else "serve"
    try:
        for settings in config["services"].values():
            port = str(settings["public_port"])
            installed.append(port)
            command(*tailscale(config), verb, "--bg", "--https=" + port, expected_proxy(config, settings))
        final = json.loads(read_command(*tailscale(config), "serve", "status", "--json"))
        check_publication(config, final)
        for settings in config["services"].values():
            port = str(settings["public_port"])
            endpoint = config["hostname"] + ":" + port
            if port not in final.get("TCP", {}) or bool(final.get("AllowFunnel", {}).get(endpoint)) != (mode == "public"):
                raise ValueError("Tailscale publication mode did not match the requested mode.")
        check_public(config)
    except (subprocess.SubprocessError, ValueError):
        for port in installed:
            subprocess.run([*tailscale(config), verb, "--https=" + port, "off"], check=False)
        raise
    print(f"Published {mode}; login and reservation required on every application/API/stream.")
    show_urls(config)


def retarget(config, vm, sessions):
    """Point the lab at a rebuilt VM; its address, accounts and state stay the lab's."""
    valid_name(vm)
    services = discover_services(vm, config["services"]["topology"]["local_port"])
    if [settings["local_port"] for settings in services.values()] != \
            [settings["local_port"] for settings in config["services"].values()]:
        raise ValueError("The gateway's listeners would move; refusing.")
    old = config["vm"]
    card = config.get("card", {})
    was_maintenance = sessions.status()["maintenance"]
    # Ends the current reservation: its views and streams belong to the old VM.
    sessions.set_maintenance(True)
    try:
        config.update(vm=vm, services=services, card=lab_card(config["lab"], vm, card.get("title"), card.get("summary")))
        save_json(CONFIG_ROOT / (config["lab"] + ".json"), config, pwd.getpwnam("easymesh-remote"))
        if subprocess.run(["nft", "list", "table", "inet", firewall_table(config)],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            firewall(config, True)
        command("systemctl", "try-restart", f"easymesh-remote@{config['lab']}.service")
    finally:
        sessions.set_maintenance(was_maintenance)
    print(f"{config['lab']} now serves {vm} (was {old}); the next reservation waits for the handoff.")


def unpublish(config):
    current = json.loads(read_command(*tailscale(config), "serve", "status", "--json"))
    check_publication(config, current)
    for settings in config["services"].values():
        port = str(settings["public_port"])
        if port in current.get("TCP", {}):
            endpoint = config["hostname"] + ":" + port
            verb = "funnel" if current.get("AllowFunnel", {}).get(endpoint) else "serve"
            command(*tailscale(config), verb, "--https=" + port, "off")
    print("Unpublished only this gateway. Direct-port protection remains enabled.")


def listen_address(lab):
    # Each lab's gateway listens on a loopback address of its own, in the range the labs' Tailscale
    # nodes may reach (easymesh-remote-tailscale@.service); 127.0.0.0/8 is local without setup.
    value = int(hashlib.sha256(lab.encode()).hexdigest()[:4], 16) % 65534 + 1
    return f"127.77.{value >> 8}.{value & 255}"


def node_status(socket, wait=20):
    deadline = time.monotonic() + wait
    while True:
        try:
            return json.loads(read_command("tailscale", "--socket", socket, "status", "--json"))
        except (subprocess.SubprocessError, ValueError):
            if time.monotonic() >= deadline:
                raise ValueError(f"The lab's Tailscale node does not answer on {socket}; see its journal.")
            time.sleep(.5)


def published(config):
    current = json.loads(read_command(*tailscale(config), "serve", "status", "--json"))
    return any(f"{config['hostname']}:{settings['public_port']}" in current.get("Web", {})
               for settings in config["services"].values())


def own_address(config, name, auth_key_file=None):
    """Give the lab a Tailscale node of its own, NAME.<tailnet>.ts.net, beside the host's."""
    if config.get("tailscale_socket"):
        raise ValueError(f"{config['lab']} already has its own address, {config['hostname']}.")
    if published(config):
        raise ValueError("Unpublish first; publish again once the lab has its own address.")
    valid_name(name)
    unit = f"easymesh-remote-tailscale@{config['lab']}.service"
    socket = f"/run/easymesh-remote-tailscale/{config['lab']}/tailscaled.sock"
    command("systemctl", "enable", "--now", unit)
    status = node_status(socket)
    if status.get("BackendState") != "Running":
        up = ["tailscale", "--socket", socket, "up", "--hostname=" + name, "--accept-dns=false"]
        if auth_key_file:
            up.append("--auth-key=file:" + auth_key_file)
        else:
            print("Open the link below in a browser signed in to the tailnet to add the lab's node.", flush=True)
        subprocess.run(up, check=True, stdin=subprocess.DEVNULL, timeout=1800)
        status = node_status(socket)
    hostname = status["Self"]["DNSName"].rstrip(".")
    if status.get("BackendState") != "Running" or not re.fullmatch(re.escape(name) + r"\.[a-z0-9-]+\.ts\.net", hostname):
        raise ValueError(f"The lab's node is {hostname or 'not running'}, not {name}.<tailnet>.ts.net: "
                         "is the name taken by another device? Rename that device, or choose --name.")
    config.update(hostname=hostname, tailscale_socket=socket, listen_address=listen_address(config["lab"]))
    save_json(CONFIG_ROOT / (config["lab"] + ".json"), config, pwd.getpwnam("easymesh-remote"))
    command("systemctl", "try-restart", f"easymesh-remote@{config['lab']}.service")
    print(f"{config['lab']} now has its own address, {hostname}. Publish it: publish --mode private")


def host_address(config):
    """Back to the host's Tailscale node; the lab's node is stopped, its identity kept for later."""
    if not config.get("tailscale_socket"):
        raise ValueError(f"{config['lab']} already uses the host's address, {config['hostname']}.")
    if published(config):
        raise ValueError("Unpublish first; publish again on the host's address.")
    status = json.loads(read_command("tailscale", "status", "--json"))
    if status.get("BackendState") != "Running":
        raise ValueError("The host's Tailscale is not running: sudo tailscale up first.")
    config.pop("tailscale_socket")
    config["hostname"] = status["Self"]["DNSName"].rstrip(".")
    save_json(CONFIG_ROOT / (config["lab"] + ".json"), config, pwd.getpwnam("easymesh-remote"))
    command("systemctl", "disable", "--now", f"easymesh-remote-tailscale@{config['lab']}.service")
    command("systemctl", "try-restart", f"easymesh-remote@{config['lab']}.service")
    print(f"{config['lab']} uses the host's address again, {config['hostname']}.")


def show_urls(config):
    for name, settings in config["services"].items():
        print(f"{name}: {public_url(config, settings)}/_remote/")


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
    card = commands.add_parser("card", help="refresh the lab card: title, summary, the VM's build date")
    for described in (setup, card):
        described.add_argument("--title")
        described.add_argument("--summary")
    adding = commands.add_parser("add-user", help="add an account or replace its password")
    adding.add_argument("username")
    adding.add_argument("--role", choices=ROLES, default="operator")
    commands.add_parser("remove-user").add_argument("username")
    role = commands.add_parser("role", help="make an account an operator or an administrator")
    role.add_argument("username")
    role.add_argument("role", choices=ROLES)
    commands.add_parser("users", help="list the accounts, their roles and who is signed in")
    address = commands.add_parser("address", help="give the lab a Tailscale node of its own (or --host: the host's)")
    address.add_argument("--name", help="the node's name; the lab's label by default")
    address.add_argument("--auth-key-file", help="a Tailscale auth key in a root-only file, instead of a login link")
    address.add_argument("--host", action="store_true", help="back to the host's node")
    publisher = commands.add_parser("publish")
    publisher.add_argument("--mode", choices=("private", "public"), default="private")
    publisher.add_argument("--confirm-public", action="store_true")
    publisher.add_argument("--close-lan", action="store_true", help="close the lab's ports to the LAN, private too")
    publisher.add_argument("--keep-lan-open", action="store_true",
                           help="a public lab whose ports stay open on the trusted LAN (local work on it goes on)")
    moving = commands.add_parser("retarget", help="serve a rebuilt VM of the lab; address, accounts and state stay")
    moving.add_argument("--vm", required=True)
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
    if args.operation in {"add-user", "remove-user", "role"}:
        username = valid_name(args.username)
        path = Path(config["users_file"])
        users = load_users(path)
        if args.operation == "add-user":
            password = getpass.getpass("Password (at least 16 characters): ")
            if len(password) < 16 or len(password) > 512 or password != getpass.getpass("Repeat password: "):
                raise ValueError("Passwords must match and contain 16..512 characters.")
            users[username] = {"password": password_hash(password), "role": args.role}
        elif args.operation == "role":
            if username not in users:
                raise ValueError(f"No account {username}.")
            users[username]["role"] = args.role
        else:
            users.pop(username, None)
        save_json(path, users, pwd.getpwnam("easymesh-remote"))
        if args.operation == "role":
            print(f"{username} is now an {args.role}; the gateway reads roles at each request.")
        else:
            sessions.revoke_user(username)
            print("Credentials updated; existing sessions for this user were revoked.")
    elif args.operation == "users":
        signed_in = {entry["username"]: entry["sessions"] for entry in sessions.signed_in()}
        for name, entry in sorted(load_users(config["users_file"]).items()):
            print(f"{name:<32} {entry['role']:<9} {signed_in.get(name, 0)} signed-in browser(s)")
    elif args.operation == "card":
        config["card"] = lab_card(config["lab"], config["vm"], args.title, args.summary)
        save_json(CONFIG_ROOT / (args.lab + ".json"), config, pwd.getpwnam("easymesh-remote"))
        print(json.dumps(config["card"], indent=2))
        print(f"Restart easymesh-remote@{args.lab} to show it.")
    elif args.operation == "address":
        if args.host:
            host_address(config)
        else:
            own_address(config, args.name or args.lab, args.auth_key_file)
    elif args.operation == "publish":
        publish(config, args.mode, args.confirm_public, args.close_lan, args.keep_lan_open)
    elif args.operation == "retarget":
        retarget(config, args.vm, sessions)
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
        units = [f"easymesh-remote@{args.lab}.service", f"easymesh-remote-firewall@{args.lab}.service"]
        if config.get("tailscale_socket"):
            units.append(f"easymesh-remote-tailscale@{args.lab}.service")
        command("systemctl", "--no-pager", "status", *units)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
        print(f"remote access: {error}", file=sys.stderr)
        sys.exit(1)
