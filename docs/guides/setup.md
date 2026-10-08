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

Each application keeps its own root paths and same-origin APIs. The lab's page,
`/_remote/` on any of the three addresses, shows them together in one window
(section "The lab's page").
Do not publish raw backends alongside the gateway, or mount these applications
under arbitrary URL prefixes. Only one lab can occupy these three ports on a
Tailscale hostname, and on the host's own name they compete with the host's other
services: give each lab its own name (section "The lab's own address").

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

LAB=rdk-emosa                      # the lab's configuration name
VM=rdk-emosa-1005                  # its VM now
REMOTE=/opt/easymesh-remote/manage.py
sudo "$REMOTE" --lab "$LAB" configure --vm "$VM"
sudo "$REMOTE" --lab "$LAB" add-user rob --role admin
sudo "$REMOTE" --lab "$LAB" add-user alice
sudo "$REMOTE" --lab "$LAB" add-user bob
```

Name the gateway after the lab's configuration, not its VM: the label keeps the
accounts, the reservation state and the address when the VM is rebuilt under a
new name, and only `--vm` changes. Give each person a separate account and a
unique password of at least 16 characters; creation prompts without placing
passwords on the command line. An account is an **operator** (reserves and uses
the lab) or an **admin** (also releases the lab, switches maintenance and sees who
is signed in, from the lab's page). `configure` records the lab card the page
shows: the configuration's title and summary (`--title`, `--summary` to change
them) and the VM's build date.
`configure` detects the authenticated Tailscale hostname and existing LXD proxy
addresses/ports; it refuses missing, wildcard or unsupported proxy definitions.
Use `configure --vm VM_NAME` when the gateway label differs from the VM name.
This initial setup targets the standard IPv4 RDK NAT proxy layout, not arbitrary
remote backends, prpl device names, or a subnet router. Discovery also records
the corresponding VM interface's IPv6 addresses for direct-access blocking;
it refuses setup when the running VM's interface inventory cannot be verified.

Configuration lives in `/etc/easymesh-remote/LAB.json`, password hashes and
roles in `LAB.users.json`, and session state in `/var/lib/easymesh-remote/LAB/`.
The gateway reads accounts and roles at each sign-in and administrative request:
`role NAME admin|operator` and `remove-user` take effect at once, `users` lists
the accounts and who is signed in, and `card` refreshes the lab card after a
rebuild (then restart the gateway).
Gateway listeners are loopback-only (`127.0.0.1`, or the lab's own address in
`127.77.0.0/16` once it has its own name); their three ports are derived from the lab
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

**A private publication leaves the lab network as it is.** The lab network is
trusted (the proposal's requirement Q3): LAN bookmarks and local suites keep
working, and whoever is on the LAN reaches the lab without a reservation, so run
local work under maintenance. `--close-lan` closes the lab's ports to the LAN all
the same. **A public publication closes them** (section "Direct-port
protection"), unless `--keep-lan-open` keeps them open: for a public lab on which
local work goes on, the trusted LAN keeping the access it had.

```sh
sudo "$REMOTE" --lab "$LAB" publish --mode private
sudo "$REMOTE" --lab "$LAB" status
sudo tailscale serve status
```

Follow Tailscale's consent link if HTTPS needs enabling. `publish` then requests
each public URL and rolls back unless this gateway answers there: another program
listening on that port of the host's Tailscale address (a web server on `*:443`,
as on rev120) takes the port from Serve, which Serve's own status does not show.
The command prints the actual three URLs; open `/_remote/` on any of them. Private visitors must have
Tailscale installed, be signed in and be allowed by tailnet policy. Complete the
two-browser checks below before making the same gateway public:

```sh
sudo "$REMOTE" --lab "$LAB" publish --mode public --confirm-public
sudo tailscale funnel status
```

Follow Tailscale's additional Funnel consent/policy flow. The public DNS name can take
longer than Tailscale's ten minutes: on rev120 it appeared after about 18. Only the gateway is
exposed, including its login page. An anonymous visitor cannot read topology,
telemetry or use controls. There is no spectator or read-only role yet: every
account can reserve the lab and drive it.
Login attempts are bounded globally and per username; this is a small trusted
collaborator service, not a hardened multi-tenant public hosting platform.

## The lab's own address

On the host's Tailscale name a lab takes the host's ports 443, 8443 and 10000, so a
host publishes one lab, and a program of the host's that listens on one of them wins it
(on rev120 Apache holds `*:443`). `address` gives the lab a Tailscale node of its own,
named after the lab: `rdk-emosa.<tailnet>.ts.net`. Its ports are its own, a host can
publish several labs, and sharing the node shares exactly one lab.

```sh
sudo "$REMOTE" --lab "$LAB" unpublish          # if it is published on the host's name
sudo "$REMOTE" --lab "$LAB" address            # prints a link: open it signed in to the tailnet
sudo "$REMOTE" --lab "$LAB" publish --mode private
```

- **What runs:** `easymesh-remote-tailscale@LAB.service`, a `tailscaled` of the lab's
  own beside the host's, in userspace networking, as a throwaway system user with no
  capabilities and a read-only system. The host's own Tailscale is not touched.
- **Its identity lives on the host** (`/var/lib/easymesh-remote-tailscale/LAB`), next to
  the gateway's state, so it survives rebuilds of the lab's VM: the lab keeps its name
  while its VM changes from `rdk-emosa-1005` to the next build.
- **What it reaches:** userspace networking forwards an inbound connection on any port
  it does not serve itself to `127.0.0.1`, which would expose every local service of
  the host on the lab's name. The unit denies loopback to the node except the resolver
  and the labs' gateways: each lab's gateway listens on its own address in
  `127.77.0.0/16` once the lab has its own name.
- **Login:** the link adds the node as a device of the person who opens it. Disable
  its key expiry in the admin console, or create the node with a tagged auth key
  instead of the link (`address --auth-key-file FILE`, the key in a root-only file);
  tagged devices do not expire.
- **The name** is the lab's label; `--name` chooses another. `address` refuses when the
  tailnet gives the node another name (the name is taken by another device).
- **Back to the host's name:** `unpublish`, then `address --host`. The node is stopped
  and its identity kept, so `address` brings the same name back.

Serve/Funnel background configurations survive host/Tailscale restarts. The
gateway and firewall units are enabled independently of the VM: they do **not**
enable VM autostart. With the VM stopped, the gateway refuses acquisition when
room availability cannot be verified. Existing unrelated Tailscale listeners
are never overwritten, and scripts never call `tailscale serve reset`.

## Reservation, activity and handoff

Sign in, then press **Reserve and open the lab**. A persistent SQLite transaction gives
exactly one browser session ownership across all three applications and their
HTTP, SSE and WebSocket endpoints. Another browser sees the owner's account,
idle countdown and hard limit; it cannot enter any view or call its APIs.
Tabs in the same browser profile share the secure, HTTP-only session cookie.
Two browsers using the same account still cannot acquire concurrently.

- **Idle timeout: 10 minutes.** Real clicks, keys, scrolling and touch activity
  in a visible, focused lab's page or any of its views renew it. **Keep active**
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

The page's bar stays above the views; full screen hides it, but genuine input in
a full-screen view still counts. On expiry the page closes every view. Opening a
backend page outside the lab's page does not install the activity detector,
although all server-side gates still apply: use the lab's page for normal
operation. Synthetic browser events do not count; a deliberately scripted API
client can send activity explicitly but cannot exceed the hard deadline.

## The lab's page

`/_remote/` on any of the lab's addresses is its front door and its workspace.

- **Before a reservation:** the lab card (what the lab is; for a signed-in
  account its VM, host and build date), whether it is free, who holds it and when
  it will be free at the latest, sign-in, reserve, the lab's views and the rules
  above.
- **During one:** every view of the lab in one window, in three layouts: **side
  by side**, **one large** with the others stacked beside it, and **one at a
  time** with tabs. Dividers are dragged to resize (double-click shares evenly),
  a tile's title bar maximizes it within the window (double-click, or its button;
  Escape restores) or shows it full screen, and each tile can be opened alone in
  a new tab. **Lab page** returns to the welcome with the views kept open.
- **The layout** is kept per lab in the browser and in the address (for example
  `#layout=tabs&view=room`), so a link opens the same arrangement.
- **An admin** has **Manage**: who is signed in and in how many browsers, release
  the lab now (the handoff delay still applies), start and end maintenance. Accounts
  are added and removed on the host only.

Each view keeps its own origin, as its application requires. The page frames
each one through a tile, `/_remote/tile` on the view's own origin, which watches
the view for genuine input (the page cannot see into another origin's frame) and
renews the reservation itself. The gateway allows the lab's own origins to frame
its views and tiles, and nothing else: it replaces any `frame-ancestors` and
`X-Frame-Options` of an application (Console NG forbids all framing) with the
lab's origins. Tiles are never moved in the page, because moving a frame reloads
its view: the layouts rearrange them in one grid.

Login expires after eight hours. Reservation state and deadlines survive gateway
restarts. There is no waiting queue or automatic takeover; after release and
handoff another user explicitly reserves the lab. The lock is access control,
not an attempt to isolate multiple independent experiments in one running mesh.

## Direct-port protection and local testing

A public `publish` (or `--close-lan`) first enables `easymesh-remote-firewall@LAB.service`; a
private one, or a public one with `--keep-lan-open`, disables it. Its dedicated
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

## After a rebuild

A rebuilt lab is a new VM with new host ports and addresses (`rdk-emosa-1005`, then
the next build's). Move the lab to it once the new VM is accepted:

```sh
sudo "$REMOTE" --lab "$LAB" retarget --vm rdk-emosa-1005
```

`retarget` reads the new VM's proxy devices, ends the current reservation (its views
belong to the old VM), records the new VM and its build date on the lab card, moves
the firewall to the new ports if the LAN is closed, and restarts the gateway. The
lab's address, accounts and sessions stay; the next reservation waits for the
handoff. Serve and Funnel are untouched: they point at the gateway, not at the VM.
`publish` refuses a lab whose VM changed under it and names `retarget`. Do not add
alternative LXD proxy devices around the protected endpoints.

## Stop sharing and restore LAN access

```sh
sudo "$REMOTE" --lab "$LAB" unpublish
sudo systemctl disable --now "easymesh-remote@$LAB.service"
```

If the LAN was closed (a public lab, or `--close-lan`), it stays closed: stopping a
gateway must not expose a lab that was meant to be closed. To open it again, after
unpublishing:

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

1. Anonymous topology/API/WebSocket requests fail; the lab's page sign-in works.
2. Alice reserves and uses room Play/drag, topology and Console NG streaming,
   side by side in the lab's page, and input in each view renews idle time.
   Bob sees **In use by alice**, with no backend access on any of the three ports.
3. Confirm genuine activity renews idle time, background polling does not, and
   expiry/release closes streams. Bob can reserve after the handoff countdown.
4. Confirm the existing local room lease blocks admission, and maintenance
   blocks remote acquisition. Restore normal access afterward.
5. With the LAN closed (public, or `--close-lan`), verify from a **different
   machine** that all three old LAN ports and direct VM service ports are
   inaccessible. Host-local diagnostics should still work.
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
checkout, and restart `easymesh-remote@LAB.service`. Check the lab's page and firewall
before disabling maintenance. No native agent, controller or VM rebuild is needed.

## Upstream references

- [Tailscale Linux installation](https://tailscale.com/docs/install/linux)
- [Serve: private HTTPS and access rules](https://tailscale.com/docs/features/tailscale-serve)
- [Funnel: public access, permitted ports and bandwidth limits](https://tailscale.com/docs/features/tailscale-funnel)
- [Serve CLI and persistent background configuration](https://tailscale.com/docs/reference/tailscale-cli/serve)
- [Userspace networking](https://tailscale.com/kb/1112/userspace-networking) and [Tailscale Services](https://tailscale.com/docs/features/tailscale-services) (the lab's own address)
- [nftables hook priorities and rule semantics](https://netfilter.org/projects/nftables/manpage.html)
