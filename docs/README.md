# The remote access documents

[Repository](../README.md) · [Site](https://vcpe.dev/easymesh-remote/)

The [site](https://vcpe.dev/easymesh-remote/) explains how a lab is reached from outside
the lab network, for a newcomer. These documents go further:

| Document | Kind | What it covers |
| --- | --- | --- |
| [Setting up the gateway](guides/setup.md) | guide | install the gateway on a lab host, configure it for a lab, add users, publish privately or publicly; reservations, the firewall, maintenance, rollback and validation |
| [What each lab publishes](reference/lab-endpoints.md) | reference | every interface of every lab configuration, how a lab publishes a port, the ways to reach one today, and what the gateway can use |
| [Remote labs](proposals/remote-labs.md) | proposal | any lab reachable: one address per lab, the lab card and a directory, an observer role and a queue, the interfaces of a lab in one window; the steps and the experiments |

Two documents elsewhere belong to the subject: the umbrella's proposal "EasyMesh labs as a
service" (a remote optimizer API, on top of remote access; easymesh-labs,
`docs/proposals/labs-as-a-service.md`) and the optimizer's reference "Local and remote
room access" (the room's own transport; easymesh-optimizer,
`docs/reference/room-access.md`).
