# Setting up the gateway: Tailscale access and exclusive sessions

[Documents](../README.md)

**Kind:** guide. This is the RDK lab's remote access manual, brought here on 2 October
2026 with the gateway it describes; only its paths changed. The RDK lab still carries the
same gateway and manual until they are removed there. The gateway supports the RDK lab's
layout only (three services, section "Install and configure"). What the other labs publish
is in [What each lab publishes](../reference/lab-endpoints.md); what is proposed beyond
this is in [Remote labs](../proposals/remote-labs.md).

## Design and scope

Install the optional gateway on the **outer LXD host**, not in the VM or radio
containers. It discovers the named RDK VM's existing topology, Console NG and
room NAT proxies. No image rebuild, VM restart, optimizer change or router port
forward is required. Existing local RF processing remains local; browser latency
must not be counted as native convergence time.

```text
Browser → Tailscale HTTPS → localhost session gateway → existing VM proxies
            private Serve        authentication        topology / console / room
            or public Funnel     one shared reservation
```

**Serve is the default:** only permitted tailnet users/devices can connect.
**Funnel is public:** visitors need only a browser, but must still sign in to the
gateway. Funnel does not authenticate visitors. Both modes use the same gateway
accounts and reservation checks; Tailscale identity headers are not trusted as
application login credentials. Tailnet grants should limit users to the gateway
ports, not grant SSH, LXD or the whole lab subnet.

| Application | Public HTTPS port | Typical existing backend |
| --- | --- | --- |
| Network topology | 443 | VM-specific base port |
| Console NG | 8443 | base + 1 |
| Room | 10000 | base + 2 |

Each application keeps its own root paths and same-origin APIs. The portal
provides links between the three external URLs and an embedded full-size view.
Do not publish raw backends alongside the gateway, or mount these applications
under arbitrary URL prefixes. Only one lab can occupy these three ports on a
Tailscale hostname; additional labs need a separate gateway node/hostname.

## Install and configure

Run from a fresh repository checkout on the **outer host** of an existing named
VM. This procedure installs packages but does not rebuild or start that VM.
The installer supports Ubuntu 22.04/24.04, installs `python3-aiohttp`, `nftables`,
`curl` and Tailscale from its signed stable APT repository, and creates a
non-login service account. It never ignores APT signature/repository failures.
Keep Ubuntu security updates and Tailscale current.

```sh
sudo bash gateway/install-host.sh
sudo tailscale up

LAB=demo-a                         # your existing VM name
REMOTE=/opt/easymesh-remote/manage.py
sudo "$REMOTE" --lab "$LAB" configure
sudo "$REMOTE" --lab "$LAB" add-user alice
sudo "$REMOTE" --lab "$LAB" add-user bob
```

Give each person a separate account and a unique password of at least 16
characters; creation prompts without placing passwords on the command line.
`configure` detects the authenticated Tailscale hostname and existing LXD proxy
addresses/ports; it refuses missing, wildcard or unsupported proxy definitions.
Use `configure --vm VM_NAME` when the gateway label differs from the VM name.
This initial setup targets the standard IPv4 RDK NAT proxy layout, not arbitrary
remote backends, prpl device names, or a subnet router. Discovery also records
the corresponding VM interface's IPv6 addresses for direct-access blocking;
it refuses setup when the running VM's interface inventory cannot be verified.

Configuration lives in `/etc/easymesh-remote/LAB.json`, password hashes in
`LAB.users.json`, and session state in `/var/lib/easymesh-remote/LAB/`.
Gateway listeners are loopback-only; their three ports are derived from the lab
name. Use `--local-port-base 41000` if those ports are occupied. Configuration
files are root-owned and group-readable by the service, never world-readable.
Passwords use salted scrypt; session tokens are random and stored only as hashes.
Do not copy these deployment credentials into VM images, thin archives or Git.

Timeouts can be chosen at configuration time:

```sh
sudo "$REMOTE" --lab "$LAB" configure \
  --idle-seconds 600 --maximum-seconds 3600 --handoff-seconds 125
```

That is an alternative to the earlier `configure`, not a second invocation.
An existing configuration is never silently overwritten. On other systemd
distributions, install equivalent dependencies and inspect the installer/unit
files; the supplied package installer deliberately refuses unsupported hosts.

## Start private; publish publicly only deliberately

**Publication blocks direct network access to this VM's three web services.**
Ordinary LAN bookmarks for those ports stop working; use the gateway instead.
Read the firewall section before running this on a shared host.

```sh
sudo "$REMOTE" --lab "$LAB" publish --mode private
sudo "$REMOTE" --lab "$LAB" status
sudo tailscale serve status
```

Follow Tailscale's consent link if HTTPS needs enabling. The command prints the
actual three URLs; open `/_remote/` on any of them. Private visitors must have
Tailscale installed, be signed in and be allowed by tailnet policy. Complete the
two-browser checks below before making the same gateway public:

```sh
sudo "$REMOTE" --lab "$LAB" publish --mode public --confirm-public
sudo tailscale funnel status
```

Follow Tailscale's additional Funnel consent/policy flow. Only the gateway is
exposed, including its login page. An anonymous visitor cannot read topology,
telemetry or use controls. Accounts have the same operator permissions: there
is no public spectator or read-only role in this initial implementation.
Login attempts are bounded globally and per username; this is a small trusted
collaborator service, not a hardened multi-tenant public hosting platform.

Serve/Funnel background configurations survive host/Tailscale restarts. The
gateway and firewall units are enabled independently of the VM: they do **not**
enable VM autostart. With the VM stopped, the gateway refuses acquisition when
room availability cannot be verified. Existing unrelated Tailscale listeners
are never overwritten, and scripts never call `tailscale serve reset`.

## Reservation, activity and handoff

Sign in, then press **Reserve lab**. A persistent SQLite transaction gives
exactly one browser session ownership across all three applications and their
HTTP, SSE and WebSocket endpoints. Another browser sees the owner's account,
idle countdown and hard limit; it cannot enter any view or call its APIs.
Tabs in the same browser profile share the secure, HTTP-only session cookie.
Two browsers using the same account still cannot acquire concurrently.

- **Idle timeout: 10 minutes.** Real clicks, keys, scrolling and touch activity
  in a visible, focused portal or embedded view renew it. **Keep session active**
  supports watching a long demonstration without dragging objects.
- **Hard maximum: 60 minutes.** Activity cannot extend this reservation deadline.
- Automatic metrics requests, status polling, open sockets and unattended Play
  do **not** renew idle time. Closing a tab needs no unreliable unload request;
  the reservation expires normally unless another tab is actively used.
- Release, logout, expiry, credential revocation or administrator release blocks
  subsequent requests and closes already-open streams within approximately one
  second. A command already delivered to the native service cannot be undone.
- **Handoff delay: 125 seconds.** This exceeds the room's supported maximum
  120-second native operator lease, allowing its normal pause/cancellation to
  occur. The gateway does not reset the world or restart services. Automatic
  native optimization and background measurements can continue.
- Before acquisition, the gateway checks the room's native lease. An existing
  local operator or unavailable/invalid lease response blocks admission.

The banner remains above the view; application fullscreen temporarily hides it,
but genuine input inside fullscreen still counts. On expiry the portal unloads
the view. Opening a backend page outside the portal does not install the activity
detector, although all server-side gates still apply. Use portal URLs for normal
operation. Synthetic browser events do not count; a deliberately scripted API
client can send activity explicitly but cannot exceed the hard deadline.

Login expires after eight hours. Reservation state and deadlines survive gateway
restarts. There is no waiting queue or automatic takeover; after release and
handoff another user explicitly reserves the lab. The lock is access control,
not an attempt to isolate multiple independent experiments in one running mesh.

## Direct-port protection and local testing

`publish` first enables `easymesh-remote-firewall@LAB.service`. Its dedicated
`inet em_remote_*` nftables table drops incoming traffic to the selected host
forward ports **before LXD DNAT**, plus direct access to the corresponding VM
addresses/ports. It does not flush existing firewall rules, change LXD proxy
devices, or affect SSH, Grafana, LXD UI, other VMs or the RF data plane.
Host-originated gateway and diagnostic connections remain possible.

Host administrators, root/LXD access, custom forwarding, and code running on
the VM itself remain trusted and can bypass the gateway. Do not give remote
operators these capabilities. Before local tests or administrative work:

```sh
sudo "$REMOTE" --lab "$LAB" maintenance-on
sudo "$REMOTE" --lab "$LAB" status
# Wait for the previous native room lease to settle; run local tests.
# Stop those tests and release their room lease before allowing remote users.
sudo "$REMOTE" --lab "$LAB" maintenance-off
```

Maintenance revokes the current gateway reservation, closes its streams and
blocks new reservations until disabled. The existing test suite does not
automatically acquire this gateway reservation. Run it from the host while
maintenance is enabled; tests from another machine cannot bypass the firewall.
Never run a mutating local campaign while a remote user owns the lab.

Inspect and recover without changing native services:

```sh
sudo journalctl -u "easymesh-remote@$LAB" -n 60 --no-pager
sudo "$REMOTE" --lab "$LAB" release
sudo "$REMOTE" --lab "$LAB" remove-user bob
```

Audit logs include sign-in, acquisition, release and timeout events, not
passwords, cookies or API bodies. Password replacement with `add-user` also
revokes that user's existing sessions. Administrator release does not skip the
handoff delay.

If a VM is rebuilt with changed IPs/ports, unpublish and stop the gateway first,
then update its configuration to the new verified proxy values. Publication
rechecks LXD definitions rather than silently pointing at stale addresses.
Do not add alternative LXD proxy devices around the protected endpoints.

## Stop sharing and restore LAN access

```sh
sudo "$REMOTE" --lab "$LAB" unpublish
sudo systemctl disable --now "easymesh-remote@$LAB.service"
```

Direct-port blocking deliberately remains: stopping a gateway must not expose
unauthenticated backends. To return to the original **trusted-LAN** arrangement,
after unpublishing:

```sh
sudo systemctl disable --now "easymesh-remote-firewall@$LAB.service"
```

This removes only the gateway-owned table. It does not remove Tailscale or
unrelated Serve/Funnel configurations. Changing a live firewall globally,
flushing rules or publishing alternate ports can invalidate protection; check
direct-port denial again after such changes.

## Validation and dependencies

Source checks require Python 3.10+, `pytest` and `aiohttp`; the HTTP tests use
temporary local servers, never the VM. Browser checks additionally need Node,
Playwright/Chromium and `openssl` for an ephemeral **test-only** TLS certificate.
The optional firewall integration check needs `nftables`, `iproute2`, `util-linux`
and unprivileged user/network namespaces; it runs entirely in disposable network
namespaces, verifying NAT/direct IPv4/IPv6 denial, host access and rollback. If
these prerequisites are unavailable it reports a skip, not a firewall pass.
The browser test finds Playwright as `playwright-core`, or at the path in
`PLAYWRIGHT_MODULE`; `CHROMIUM_PATH` names a Chromium to use instead of Playwright's own.

```sh
python3 -m pytest -q tests/test_remote_access.py
node tests/remote-access-browser-test.js
```

After installation, verify with two different browser profiles/accounts:

1. Anonymous topology/API/WebSocket requests fail; the portal login works.
2. Alice reserves and uses room Play/drag, topology and Console NG streaming.
   Bob sees **In use by alice**, with no backend access on any of the three ports.
3. Confirm genuine activity renews idle time, background polling does not, and
   expiry/release closes streams. Bob can reserve after the handoff countdown.
4. Confirm the existing local room lease blocks admission, and maintenance
   blocks remote acquisition. Restore normal access afterward.
5. From a **different machine**, verify all three old LAN ports and direct VM
   service ports are inaccessible. Host-local diagnostics should still work.
6. Verify session persistence across a gateway restart; no native process or VM
   should restart. Check fullscreen, SSE/WebSocket reconnect and logout.
7. Only then enable Funnel and repeat from a non-tailnet internet connection.

The gateway preserves the external Host/Origin and HTTPS context, proxies SSE
without whole-response buffering, supports bidirectional WebSockets and does not
cache lab responses. It strips its own cookie and supplied Tailscale identity
headers before forwarding. Existing same-origin write protections remain.
Future API clients use the same login/reservation/activity flow, with HTTPS
Origin headers for writes; no separate unauthenticated API publication is needed.

For gateway-only upgrades, enable maintenance, rerun the installer from the new
checkout, and restart `easymesh-remote@LAB.service`. Check the portal and firewall
before disabling maintenance. No native agent, controller or VM rebuild is needed.

## Upstream references

- [Tailscale Linux installation](https://tailscale.com/docs/install/linux)
- [Serve: private HTTPS and access rules](https://tailscale.com/docs/features/tailscale-serve)
- [Funnel: public access, permitted ports and bandwidth limits](https://tailscale.com/docs/features/tailscale-funnel)
- [Serve CLI and persistent background configuration](https://tailscale.com/docs/reference/tailscale-cli/serve)
- [nftables hook priorities and rule semantics](https://netfilter.org/projects/nftables/manpage.html)
