#!/usr/bin/env bash
set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "Run sudo bash $0 on the outer LXD host." >&2; exit 1; }
source /etc/os-release
case "${ID:-}:${VERSION_ID:-}" in
    ubuntu:22.04|ubuntu:24.04) ;;
    *) echo "This installer supports Ubuntu 22.04/24.04; see the manual for other systemd hosts." >&2; exit 2 ;;
esac
root=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
apt-get update
apt-get install -y python3 python3-aiohttp nftables curl ca-certificates
if ! command -v tailscale >/dev/null; then
    curl --fail --silent --show-error --location \
        "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.noarmor.gpg" \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl --fail --silent --show-error --location \
        "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.tailscale-keyring.list" \
        -o /etc/apt/sources.list.d/tailscale.list
    apt-get update
    apt-get install -y tailscale
fi
if ! id easymesh-remote >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /nonexistent --shell /usr/sbin/nologin easymesh-remote
fi
install -d -m 0755 /opt/easymesh-remote /opt/easymesh-remote/web
install -d -m 0750 -o root -g easymesh-remote /etc/easymesh-remote
install -d -m 0755 /var/lib/easymesh-remote
install -m 0644 "$root/gateway.py" "$root/remote_state.py" /opt/easymesh-remote/
install -m 0755 "$root/manage.py" /opt/easymesh-remote/manage.py
install -m 0644 "$root"/web/* /opt/easymesh-remote/web/
install -m 0644 "$root"/*.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now tailscaled
printf '%s\n' 'Installed only; no lab exposed or reserved, no VM changed.' \
    'Next: sudo tailscale up, then configure/add-user/publish with /opt/easymesh-remote/manage.py.'
