# Remote labs

[Documents](../README.md)

**Status:** Proposal. Nothing in sections 4 to 9 is implemented. What exists is the gateway
([setup](../guides/setup.md)) for the RDK lab. **Prepared:** 2 October 2026.

## 1. The goal

Any lab can be reached and used from anywhere, by someone who is not on the lab network
and has no account on a lab host:

- **accessibility:** a lab's interfaces and APIs open in a browser, safely;
- **selection:** it is clear which labs exist, what each is and whether it is free;
- **concurrency:** two people never drive one mesh at the same time, and both can see why;
- **one window:** the several interfaces of one lab are visible together, arranged by the
  viewer.

The physical protocol lab is the weakest candidate: its panel can be reached, its hardware
cannot be moved.

**The rule: build on what the labs already do.** The labs publish their interfaces the
same way, and one lab has a working gateway. No step here replaces either.

## 2. What exists

| Piece | Where | What it gives |
| --- | --- | --- |
| Published ports | every lab VM: LXD proxy devices | each interface on a host address and port, unauthenticated ([reference](../reference/lab-endpoints.md)) |
| The gateway | this repository, `gateway/` | login, one exclusive reservation across a lab's interfaces, Tailscale Serve (private) or Funnel (public), a firewall for the direct ports, a portal that embeds one interface |
| The room's own lease | the room service | one operator in the room at a time, 120 seconds at most |
| Labs as a service | the umbrella's proposal of 26 September 2026 | a remote optimizer: an API and a Python library, on top of remote access |

## 3. What is missing

| Gap | Today |
| --- | --- |
| Other labs | the gateway requires the RDK lab's three device names and a room |
| More than one lab per host | one Tailscale hostname carries one lab |
| Knowing what exists | the labs poster, updated by hand |
| Watching without driving | every account is an operator; a second person sees only "in use" |
| Waiting | no queue; the next person tries again |
| One window | the portal shows one interface at a time |
| Suites and people | the suites do not take the reservation; an administrator switches maintenance on by hand |

## 4. Accessibility

Three kinds of people need three different doors.

| Who | Needs | Door |
| --- | --- | --- |
| Operator | builds, `lxc`, the suites, logs | SSH to the host over the private network; nothing new |
| Lab user | the lab's interfaces and APIs, to drive a room | the gateway, with a reservation |
| Spectator | to watch a room someone else drives | the gateway, read-only (section 6) |

### The network

| Option | For | Against |
| --- | --- | --- |
| A. Tailscale (what the gateway uses) | works behind any router with nothing forwarded; private by default; Funnel for a public demonstration; already built and tested here | every private visitor installs Tailscale; plan limits on users (not checked) |
| B. Cloudflare Tunnel with Access | nothing to install for a visitor; each interface gets its own host name, which the interfaces need; sign-in by e-mail | the domain's DNS must be at Cloudflare; traffic and TLS pass through a third party; the gateway's publication code would be rewritten |
| C. SSH tunnels | exists | one person, one port, an account on the host |
| D. An own reverse proxy on a rented server | full control | a server to run and secure |

**Recommendation: A.** It is built, and the gateway keeps its own login on top of it, so
the network is not the only lock.

### One address per lab

Today a lab is reached as its host's Tailscale name on three fixed ports, so a host can
publish one lab. If each **lab** has its own name, `rdk-1001` is an address:

- a host publishes as many labs as it runs;
- sharing a name shares exactly one lab;
- a lab may use any number of ports on its own name, so labs with more than three
  interfaces fit.

Three ways to get a name per lab, to be tried (E1):

1. Tailscale's named services on the host's node;
2. one more Tailscale instance per lab on the host, in userspace mode;
3. Tailscale inside the lab VM. This puts an access component into the lab image, which
   the gateway has avoided so far.

Public access through Funnel stays limited to three ports per name whatever is chosen.

## 5. Selection

### The lab card

Everything needed to list a lab already sits on its VM or is known to its build script.
The proposal is to record it in one place, as `user.*` keys on the VM, set by each lab's
build:

| Key | Example | From |
| --- | --- | --- |
| `user.lab.configuration` | `rdk`, `prplmesh`, `rdk-emosa`, `emosa-osl`, `physical` | the build script |
| `user.lab.title` | RDK optimizer lab | the build script |
| `user.lab.built` | `2026-10-01` | the build script |
| `user.lab.pins` | the commits the lab was built from | the build script |
| `user.lab.endpoint.<device>` | `room: the live room` | the build script, one per proxy device |

The endpoints themselves need no key: they are the VM's proxy devices. State (running,
stopped) is LXD's. Who holds the lab is the gateway's. A host's list of labs is then one
query of LXD, with no database to keep in step.

### The directory

- **Per host:** a page served by the gateway that lists the host's labs from their cards:
  what each is, whether it runs, whether it is free and who holds it, and a link to open it.
- **Across hosts:** later, one page that reads each host's list. It can be static if the
  hosts answer over HTTPS with their Tailscale names.
- The labs poster on the umbrella's site stays the public description. The directory is
  the live one, and private.

## 6. Concurrency

A mesh is shared state: one medium, one controller, one set of clients. Sharing a lab
between two operators cannot work; the reservation that exists is the right model.

| Addition | What it is |
| --- | --- |
| Observer role | an account or a link that may read every interface and call nothing that changes state; many at once, next to the one operator |
| Queue | "next: bob", visible to both; the lab goes to the next person after the handoff delay |
| More labs | a second person who wants to drive gets another lab VM; the directory may start a stopped one when the host has the memory and CPU for it |
| Suites | a suite takes the same reservation under its own name instead of maintenance mode switched on by hand |
| A known start | optionally, the lab is brought back to its accepted state when a reservation ends |

The observer role needs the gateway to know which requests change state. For the room that
is the HTTP method and the lease; for the controller's interface it needs a list.

## 7. One window

A **workspace** page in the portal: each interface of the lab is a tile in one browser
window.

- tiles can be moved, resized, zoomed and closed; a layout is kept per lab configuration
  and can be shared as a link;
- each interface stays on its own origin (its own port on the lab's name), as the
  interfaces require; the workspace only frames them;
- the gateway already sets the framing headers of what it proxies, so it can allow its own
  portal and nothing else;
- the moving and resizing is an existing library's job (a tiling or grid library), not
  code written here.

| Alternative | For | Against |
| --- | --- | --- |
| A ready-made dashboard (the homelab kind) | a service list, status checks and framed tiles with no code | no reservation; a second account system |
| A remote browser: a kiosk browser on the host, streamed (noVNC, Guacamole) | every interface works unchanged; several people see the same screen | video latency; no API access; one screen for all |

## 8. Steps

Each step leaves the gateway working for the RDK lab as it does today.

| Step | Adds | Done when |
| --- | --- | --- |
| 1 | this repository: the gateway and its tests moved, the setup guide, the endpoint reference | the tests pass here; the RDK lab's copy is removed |
| 2 | endpoints from the VM's proxy devices instead of three fixed names; the room check only where a room exists | the prplMesh lab and the OpenSync + EMOSA lab are reachable through the gateway |
| 3 | one address per lab | two labs on one host are published at once |
| 4 | the lab card and the per-host directory | a newcomer finds a free lab without asking |
| 5 | the observer role | a second person watches a room being driven |
| 6 | the workspace | a lab's interfaces are arranged in one window |
| 7 | the queue, the suites' reservation, starting a lab on demand | two people alternate without talking to each other |

## 9. Experiments

| | Question | How |
| --- | --- | --- |
| E1 | Which way gives one Tailscale name per lab with the least added to a host? | try the three in section 4 on one host with two VMs |
| E2 | Do all interfaces work when framed by the portal from another port? | frame the room, the controller's interface and Console NG together, behind the gateway |
| E3 | Which requests change state in each interface? | record a session of each, list the methods and paths |
| E4 | How long does a lab take from stopped to usable? | start each lab VM and time it to a healthy room |
| E5 | What does the gateway cost under a room's event stream and several observers? | one operator and ten observers on a 50-client room |

## 10. Open questions

1. **The first remote users:** in-house, named partners, or anyone. It decides between
   sharing private names and public access, and how many accounts are needed.
2. **Accounts:** the gateway's own, per lab, as today; or one set per host; or the
   network's identity.
3. **Which hosts serve remote users.** A host that builds images is busy; the lab
   configurations reference notes that two labs on one host cannot build or test at once.
4. **Operators:** whether SSH over the private network is enough, or the directory should
   also start, stop and rebuild labs.
5. **The physical lab:** publish its panel read-only, or leave it out.
