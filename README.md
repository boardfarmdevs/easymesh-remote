# easymesh-remote: reaching a lab from anywhere

<!-- labs block: the same in every repository of the EasyMesh labs, but for the Site line -->
**Site:** <https://vcpe.dev/easymesh-remote/>
The [EasyMesh labs](https://mesh.vcpe.dev/) serve three
goals: EasyMesh optimizer development
([easymesh-optimizer](https://vcpe.dev/easymesh-optimizer/)) in a rich
virtual lab, on both stacks
([RDK EasyMesh](https://vcpe.dev/meta-cmf-bananapi-vcpe/),
[prplMesh](https://vcpe.dev/prplmesh-lab/)); unchanged OpenSync
pods as EasyMesh agents under a local controller, without the OpenSync cloud
([EMOSA](https://vcpe.dev/emosa-lab/), with the
[OpenSync lab](https://vcpe.dev/opensync-lab/)'s pods); and
EasyMesh on physical hardware
([Protocol lab](https://vcpe.dev/easymesh-lab/)). Two core
components carry them: the RF medium
([easymesh-medium](https://vcpe.dev/easymesh-medium/)) and EMOSA's
OVSDB ⇄ EasyMesh conversion. The rest is infrastructure, tools (the
[room builder](https://vcpe.dev/easymesh-room-builder/)) and learning
around them.
<!-- /labs block -->

How a lab is reached by someone who is not on the lab network: the gateway that puts a
login and one reservation in front of a lab's web interfaces, how to set a lab host up
for it, what each lab publishes, and what is proposed beyond it.

The gateway runs on the **lab host**, next to the lab's VM, not in it. It installs
without rebuilding or restarting a lab. It supports the RDK lab's layout today.

```
Browser -> Tailscale HTTPS -> the gateway on the host -> the lab VM's published ports
           private (Serve)    login, one reservation     the controller's interface,
           or public (Funnel)                            Console NG, the room
```

## Components

| Part | What it is |
| --- | --- |
| [gateway/](gateway) | the gateway: `gateway.py` (the proxy and its portal, for HTTP, event streams and WebSockets), `manage.py` (configure, users, publish, maintenance, firewall), `remote_state.py` (sessions and the reservation, in SQLite), `install-host.sh`, the systemd units and the portal's web files |
| [tests/](tests) | the gateway's tests: sessions, publication, the firewall in disposable network namespaces, the proxy, and a browser test of the portal with two accounts |
| [docs/](docs) | the setup guide, the reference of what each lab publishes, the proposal |
| [site/](site) | the explainer site |
| [pages/](pages) | the site's build and the labs' shared checks |

The gateway and its tests were brought here from the RDK lab
([meta-cmf-bananapi-vcpe](https://vcpe.dev/meta-cmf-bananapi-vcpe/), `gen/remote-access`)
on 2 October 2026, unchanged but for the tests' paths. The RDK lab still carries its copy
until it is removed there.

## Getting started

On a lab host, with a named RDK lab VM running ([the guide](docs/guides/setup.md) has every
step and its checks):

```sh
git clone git@github.com:boardfarmdevs/easymesh-remote.git
cd easymesh-remote
sudo bash gateway/install-host.sh          # packages, the service account, the units
sudo tailscale up
sudo /opt/easymesh-remote/manage.py --lab rdk-1001 configure
sudo /opt/easymesh-remote/manage.py --lab rdk-1001 add-user alice
sudo /opt/easymesh-remote/manage.py --lab rdk-1001 publish --mode private
```

To run the tests, on any machine:

```sh
python3 -m venv .venv && . .venv/bin/activate
python -m pip install aiohttp pytest
python -m pytest -q tests/test_remote_access.py
npm install --no-save playwright-core && npx playwright-core install chromium
node tests/remote-access-browser-test.js
```

## Documentation

The [site](https://vcpe.dev/easymesh-remote/) explains remote access for a newcomer. The
documents are indexed in [docs/README.md](docs/README.md): the setup guide, what each lab
publishes, and the proposal for remote labs.
