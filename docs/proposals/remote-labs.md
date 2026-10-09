# Remote labs

[Documents](../README.md)

**Status:** Proposal, partly built: the gateway ([setup](../guides/setup.md)) for the RDK
lab's layout, with the lab's page (section 7), a Tailscale name per lab (section 4) and
moving a lab to its rebuilt VM (steps 2 to 4 of section 8). No lab is published since 9 October:
`rdk-emosa` on rev120 was public through Funnel until then, unpublished at the owner's word while its
VM is retired for the next RDK lab of record. The
requirements are confirmed (section 1); steps 5 to 9 are not implemented.

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

### Requirements (confirmed 6 October)

| # | Question | Answer | What it means |
| --- | --- | --- | --- |
| Q1 | Who uses a lab remotely? | the team now, all on the tailnet; named partners later | the team signs in with its Tailscale identity, no second password; the gateway's own accounts stay for people outside the tailnet |
| Q2 | What do they do? | click through the interfaces now; run their own code against a lab soon (optimizer development) | programs get keys and reservations of hours; this comes before the directory across labs |
| Q3 | Is the lab network trusted? | yes | a lab's ports close to the LAN only when it is public (or on request); local tools and suites take the reservation like everyone else |
| Q4 | After a rebuild, which VM is the lab? | the configuration's newest accepted VM | the gateway moves to it with one command (`retarget`), ideally run by the lab's build; the lab keeps its address, accounts and state |
| Q5 | Public access? | at the owner's word | `rdk-emosa` was public through Funnel until 9 October, its LAN ports kept open (`--keep-lan-open`) while local work goes on; the gateway's sign-in is the only lock, so only named accounts with strong passwords |

### Who does what

| Who | Does | Through | Today |
| --- | --- | --- | --- |
| A team member | opens the lab's address, sees whether it is free, reserves it, works in every interface in one window, releases it | the lab's page | built; signs in with a gateway account until Q1's Tailscale sign-in |
| An admin | the same, and sees who is signed in, releases the lab, switches maintenance | the lab's page, **Manage** | built |
| An operator on the host | installs the gateway, gives a lab its address, adds people, publishes, moves the lab to a rebuilt VM, runs local work under maintenance | `manage.py` over SSH | built |
| A developer's program | reserves a lab for hours and drives its APIs | a key | Q2: next |
| A watcher | follows a room someone else drives | the lab's page, read-only | observer role: later |
| A partner | the same as a team member, from outside the tailnet | Funnel (a browser and an account), or a shared lab device | possible: an admin adds the account on the host |

## 2. What exists

| Piece | Where | What it gives |
| --- | --- | --- |
| Published ports | every lab VM: LXD proxy devices | each interface on a host address and port, unauthenticated ([reference](../reference/lab-endpoints.md)) |
| The gateway | this repository, `gateway/` | login, one exclusive reservation across a lab's interfaces, Tailscale Serve (private) or Funnel (public), a firewall for the direct ports, a Tailscale name per lab, the lab's page (section 7), operator and admin accounts, `retarget` to a rebuilt VM |
| The room's own lease | the room service | one operator in the room at a time, 120 seconds at most |

## 3. What is missing

| Gap | Today |
| --- | --- |
| Other labs | the gateway requires the RDK lab's three device names and a room |
| Two labs on one host | each lab has its own name, but two on one host have not run together |
| Rebuilds | the builds do not run `retarget` yet: an operator does |
| Knowing what exists | the labs' posters and the [status page](https://vcpe.dev/easymesh-resources/lab-configurations/), updated by hand |
| Watching without driving | every account is an operator; a second person sees only "in use" |
| Waiting | no queue; the next person tries again |
| Suites and people | the suites do not take the reservation; an administrator switches maintenance on by hand or from the lab's page |

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

On its host's Tailscale name a lab takes the host's ports, so a host can publish one lab,
and the host's own programs compete for them: on rev120, Apache on `*:443` holds port 443
of the host's Tailscale address, so the controller's interface there answered with
Apache's expired certificate (6 October). If each **lab** has its own name:

- a host publishes as many labs as it runs;
- sharing a name shares exactly one lab;
- a lab may use any number of ports on its own name, so labs with more than three
  interfaces fit;
- the host's other services never meet the lab's ports.

**Built (6 October): a Tailscale node per lab, on its host, run by the gateway**
(`manage.py address`; [setup](../guides/setup.md), "The lab's own address"). The three
ways of E1, weighed on rev120 (Tailscale 1.102):

| Way | Verdict |
| --- | --- |
| 1. Tailscale Services (`tailscale serve --service=svc:NAME`) on the host's node | not now: a Service host must be a tagged device, so rev120 would stop being its owner's device; each Service is defined and its host approved in the admin console; the documentation names no Funnel for Services |
| 2. A Tailscale instance per lab on the host, in userspace networking | **chosen**: nothing changes on the host's own node; the lab's ports exist only inside its own `tailscaled`, whatever else listens on the host; one unit and one login per lab |
| 3. Tailscale inside the lab VM | no: below |

Way 2 has one trap, found in Tailscale's source: in userspace networking an inbound
connection on a port the node does not serve itself is forwarded to `127.0.0.1`, so every
local service of the host (SSH, a web server) would answer on the lab's name. The lab's
node runs under systemd with loopback denied but for the resolver and the labs' gateways;
each lab's gateway listens on its own loopback address in `127.77.0.0/16`.

**Why not Tailscale in the lab VM.** It would give the lab an address that travels with
it, but:

- every port listening in the VM (the controller's API on 8888 has no login, the room,
  SSH, the containers' ports) would be on the tailnet, held back only by tailnet policy;
- the gateway, the only lock on the interfaces, would have to move into the VM, which its
  own guide counts as trusted and able to bypass it, or be skipped;
- every build would need a Tailscale key, and the labs keep credentials out of images;
- `tailscaled` would change the firewall and DNS of a VM whose networking (bridges, NAT,
  the containers' networks) is what the lab tests;
- each rebuild would be a new device on the tailnet, and a VM's name changes with each
  rebuild.

On the host, the identity belongs to the lab and survives its rebuilds; nothing enters
the image.

### Identities, names and accounts

| What | Tailscale identity | Name | Reached by |
| --- | --- | --- | --- |
| A lab | a node of its own on its host, run by the gateway (`address`) | the lab's configuration: `rdk-emosa`, `rdk`, `prpl`, `emosa-osl`; `<configuration>-<host>` only if one configuration runs on two hosts at once | lab users, on 443, 8443 and 10000 only, through the gateway's sign-in |
| A host | its own node, as now | the host: `rev120` | operators: SSH |
| A person on the team | a tailnet member | their tailnet login | every lab node |
| A person outside | none: a lab node is shared with them | | that lab only |
| A gateway account | none: kept by the lab's gateway (`LAB.users.json`) | the person's short name, the same on every lab: `rob`, `alice` | an operator reserves and drives; an admin also releases, maintains and sees who is signed in |

- **The gateway is named after the configuration** (`--lab rdk-emosa --vm rdk-emosa-1005`),
  so its accounts, reservation state and Tailscale identity survive a rebuild; only
  `--vm` and the card change (`card`).
- **Tailnet policy**, once the tailnet has more people than its owner: lab nodes tagged
  `tag:lab` (a tagged auth key: `address --auth-key-file`; tagged devices do not
  expire); members granted `tag:lab` on ports 443, 8443 and 10000 only; hosts' SSH for
  operators only; the `funnel` attribute only on a lab meant to be public.
- **Accounts stay the gateway's own**, not the tailnet's identity: Funnel visitors have
  none, and the tailnet's identity headers are not trusted for sign-in. One account set
  per host shared by its labs is open question 2.

Public access through Funnel stays limited to three ports per name, now per lab.

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

**Built (6 October)** as the lab's page, `/_remote/` on any of the lab's addresses
([setup](../guides/setup.md), "The lab's page"): a welcome and management window before a
reservation, and every interface of the lab in one window during it.

- **The welcome:** the lab card, whether the lab is free, who holds it and when it is
  free at the latest, sign-in and reserve, the interfaces, the rules. An admin account
  also manages the lab there: who is signed in, release, maintenance.
- **The workspace:** three layouts (side by side; one large with the others stacked; one
  at a time with tabs), dividers dragged to resize, any tile maximized in the window or
  full screen or opened alone in a tab; the layout kept per lab and in the address, so it
  can be shared as a link.
- **Origins:** each interface stays on its own origin, as the interfaces require. The page
  frames it through a tile on that origin (`/_remote/tile`), which sees the interface's
  genuine input and renews the reservation: the page itself cannot see into another
  origin's frame. The gateway lets the lab's own origins frame what it proxies and nothing
  else, replacing an interface's own `frame-ancestors` (Console NG: `'none'`).
- **Changed from the first plan:** tiles are chosen layouts with resizable dividers, not
  free-moving tiles from a library. Moving a frame in a page reloads it, which would
  restart the room's view and its streams at every move; the layouts rearrange the tiles
  in one grid without moving them. Zoom is left to each interface (the room and Console
  NG have their own).

| Alternative | For | Against |
| --- | --- | --- |
| A ready-made dashboard (the homelab kind) | a service list, status checks and framed tiles with no code | no reservation; a second account system |
| A remote browser: a kiosk browser on the host, streamed (noVNC, Guacamole) | every interface works unchanged; several people see the same screen | video latency; no API access; one screen for all |

## 8. Steps

Ordered by the requirements of section 1 (renumbered 6 October). Each step leaves the
gateway working for the labs it already serves.

| Step | Adds | Done when |
| --- | --- | --- |
| 1 | this repository: the gateway and its tests moved, the setup guide, the endpoint reference | the tests pass here; the RDK lab's copy is removed. **Done** |
| 2 | one address per lab (section 4) | **built** (6 October): `rdk-emosa` on rev120 has its own name |
| 3 | the lab's page: the welcome, every interface in one window, admins (section 7) | **built** (6 October) |
| 4 | rebuilds and the trusted LAN (Q3, Q4): `retarget` to a rebuilt VM; the LAN closed only for a public lab | **built** (6 October); next, the labs' builds run `retarget` themselves |
| 5 | the team's Tailscale sign-in (Q1) | a team member opens the lab's page and is signed in |
| 6 | programs (Q2): keys, reservations of hours, the suites' reservation | an optimizer run from a developer's machine holds a lab for an afternoon; a room suite reserves the lab instead of maintenance |
| 7 | the other labs: endpoints from the VM's proxy devices instead of three fixed names, the room check only where a room exists | the prplMesh lab and the OpenSync + EMOSA lab are reachable through the gateway |
| 8 | the directory across labs, from each gateway's state (section 5) | a newcomer finds a free lab without asking |
| 9 | the observer role, the queue, starting a lab on demand (section 6) | a second person watches a room being driven; two people alternate without talking to each other |

## 9. Experiments

| | Question | How |
| --- | --- | --- |
| E1 | Which way gives one Tailscale name per lab with the least added to a host? | try the three in section 4 on one host with two VMs. **Decided (6 October):** a Tailscale instance per lab on the host (section 4); Services need a tagged host, Tailscale in the VM exposes the VM |
| E2 | Do all interfaces work when framed by the portal from another port? | frame the room, the controller's interface and Console NG together, behind the gateway. **Answered (6 October), yes:** in the RDK lab the three ran side by side through the gateway, each on its own origin, with their streams (the room's events, the controller's and Console NG's WebSockets), once the gateway replaced Console NG's `frame-ancestors 'none'` |
| E3 | Which requests change state in each interface? | record a session of each, list the methods and paths |
| E4 | How long does a lab take from stopped to usable? | start each lab VM and time it to a healthy room |
| E5 | What does the gateway cost under a room's event stream and several observers? | one operator and ten observers on a 50-client room |

## 10. Open questions

1. **The first remote users:** answered (Q1): the team now, named partners later.
2. **Accounts:** answered (Q1): the team's Tailscale identity; the gateway's own accounts
   for people outside the tailnet. Open: one set of those per host, shared by its labs.
3. **Which hosts serve remote users.** A host that builds images is busy; the lab
   configurations reference notes that two labs on one host cannot build or test at once.
4. **Operators:** whether SSH over the private network is enough, or the directory should
   also start, stop and rebuild labs.
5. **The physical lab:** publish its panel read-only, or leave it out.
