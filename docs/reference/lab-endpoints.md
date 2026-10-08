# What each lab publishes

[Documents](../README.md)

**Kind:** reference. **Read from the labs' sources on:** 2 October 2026. Which VM runs on
which host, and its name, is in the umbrella's lab configurations
([easymesh-labs](https://mesh.vcpe.dev/)); this document names no VM, because every
rebuild renames them.

Remote access starts from what a lab already offers on its host. This document lists it:
every web interface and API of every lab configuration, how the lab publishes it, and what
the gateway can use of it today.

## 1. How a lab publishes a port

A lab is one LXD virtual machine on a lab host. A service in the VM is published on the
host by an **LXD proxy device in NAT mode** on the VM: the host listens on one of its own
addresses and a port, and forwards to the VM's address and port. Every lab does it this
way.

```sh
lxc config device add "$VM" room-demo-viewer proxy nat=true \
    listen=tcp:<host address>:<host port> connect=tcp:<VM address>:8891
```

The proxy devices of a VM are therefore the list of what that lab offers. On a lab host:

```sh
lxc query /1.0/instances/"$VM" | python3 -c '
import json, sys
for name, d in json.load(sys.stdin)["expanded_devices"].items():
    if d.get("type") == "proxy":
        print(name, d["listen"], "->", d["connect"])'
```

Nothing on the host authenticates these ports. Whoever reaches the host's address reaches
the service.

## 2. The labs

The configurations are the six of the umbrella's lab configurations reference; the OpenSync
lab on its own (#3) is the base of #4 and has no VM of its own.

### RDK EasyMesh lab (#1) and RDK lab + EMOSA (#5)

| Proxy device | In the VM | What it is | On the host |
| --- | --- | --- | --- |
| `easymesh-webui` | 8888 | the controller's web interface and API (em_cli): topology, clients, steering | the base port |
| `wmediumd-console` | 8890 | Console NG: the medium, live, read-only | base + 1 |
| `room-demo-viewer` | 8891 | the room: its viewer, its API and its event stream | base + 2 |

The three host ports are a base and the next two; the base is derived from the VM's name
(`EASYMESH_PORT_BASE`), so two VMs on one host do not collide.

### prplMesh lab (#2)

| Proxy device | In the VM | What it is | On the host |
| --- | --- | --- | --- |
| `controller-ui` | 8091 | the controller dashboard | the base port |
| `wmediumd-console` | 8090 | Console NG | base + 1 |
| `room-demo-viewer` | 8891 | the room | base + 2 |

The same scheme (`PRPLMESH_PORT_BASE`, then +1 and +2). The lab reserves base +3 for the
LXD web interface. The topology adapter (8092 in the VM) is not published.

### OpenSync + EMOSA, prplMesh controller (#4)

| Proxy device | In the VM | What it is | On the host, by default |
| --- | --- | --- | --- |
| `em-ui` | 8093 | the EasyMesh controller's interface | 8660 |
| `noc-ui` | 8640 | local-noc, the pods' cloud | 8640 |

No room and no Console NG: this lab has no room service.

### Physical protocol lab (#6)

| Proxy device | In the VM | What it is | On the host, by default |
| --- | --- | --- | --- |
| `lab-panel` | 8765 | the teaching panel | 8765 |
| `lab-openspeedtest` | 3000 | the speed test server | 8766 |

Reaching the panel remotely is possible; the lab itself is tied to the hardware plugged
into its host.

## 3. What a lab host also runs

| Service | Where | Who it is for |
| --- | --- | --- |
| SSH | every host | operators: builds, `lxc`, the suites |
| LXD | every host | operators only; full control of every VM |
| Lab monitoring (Prometheus, Grafana) | optional, from the medium's `lxd-monitoring` | operators |
| The Yocto builds | rev140 | operators |

None of these belongs behind the gateway. A remote operator reaches them over SSH.

## 4. Reaching a lab today

| Way | Needs | Gives |
| --- | --- | --- |
| On the lab network | to be on the hosts' LAN | every published port, unauthenticated |
| SSH tunnel | an SSH account on the host | one forwarded port per tunnel, for one person |
| The gateway | the gateway installed on the host, a Tailscale connection or its public address, an account | the three RDK interfaces, behind a login and one reservation |

The SSH tunnel, for one interface, run on your own machine:

```sh
ssh -N -L 127.0.0.1:8891:<host address>:<room port> <user>@<host>
```

Then open `http://127.0.0.1:8891/`. The destination is the host's forwarded address and
port, not the VM's.

## 5. What the gateway uses

The gateway's `configure` reads the VM's proxy devices, and today it looks for exactly
three names and gives each a fixed public port.

| Gateway service | Proxy device it requires | Public port |
| --- | --- | --- |
| `topology` | `easymesh-webui` | 443 |
| `console` | `wmediumd-console` | 8443 |
| `room` | `room-demo-viewer` | 10000 |

| Lab | Works with the gateway today | Why not |
| --- | --- | --- |
| RDK EasyMesh, RDK + EMOSA | yes | |
| prplMesh | no | its controller interface is `controller-ui`, not `easymesh-webui` |
| OpenSync + EMOSA | no | two other devices, and no room to check the lease of |
| Physical protocol lab | no | two other devices, and no room |

The three public ports are the only ones Tailscale Funnel offers, which is why one
Tailscale hostname carries one lab; `address` gives a lab a Tailscale name of its own
([setup](../guides/setup.md), "The lab's own address"). Before a reservation the gateway also asks the room
whether a local operator holds it (`/api/demo/interactions`), so a lab without a room
cannot be reserved as the code stands.

## 6. Checked

The device names, guest ports and default host ports above are read from the build scripts.
On the two labs published today (`rdk-emosa` on rev120, public through Funnel with its LAN
ports kept open; `rdk-emosa-rev150` on rev150, private on the tailnet), checked over the
tailnet and, for the public one, from outside it: valid certificates on 443, 8443 and 10000;
423 on every interface for an anonymous visitor; sign-in, a reservation and the three
interfaces side by side for an account; on the lab's name no other port of the host answers
(SSH, the web server, printing and the gateway's own are refused; on a public lab port 80 is
Tailscale's relay, redirecting to HTTPS). Moving a lab to its rebuilt VM (`retarget`) keeps
its address, accounts and state, checked on `rdk-emosa`'s move to its rebuild.

Not checked: the prplMesh lab's and the OpenSync + EMOSA lab's interfaces through the gateway
(they come with step 7 of the [proposal](../proposals/remote-labs.md)).
