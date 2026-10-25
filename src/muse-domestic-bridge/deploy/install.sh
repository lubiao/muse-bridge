#!/usr/bin/env bash
# Install one selected channel. Never start the service or install credentials.
set -Eeuo pipefail
umask 022

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
    cat <<'USAGE'
Usage: sudo bash deploy/install.sh --channel weixin|feishu|qq [--check]

Ubuntu 24.04, Python >= 3.11, running systemd required.
Install python3-venv first if needed. No apt, firewall, login or bot runtime
installation is performed. Only the selected channel's Python dependencies
are installed. --check validates prerequisites without changing the host.
Stop muse-bridge.service explicitly before upgrading. Releases are retained.
USAGE
}
channel=''
check_only=0
while (($#)); do
    case "$1" in
        --channel) (($# >= 2)) || fail '--channel needs a value'; channel=$2; shift 2 ;;
        --check) check_only=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) fail "Unknown option: $1" ;;
    esac
done
case "$channel" in weixin|feishu|qq) ;; *) fail 'Choose exactly one --channel: weixin, feishu or qq' ;; esac
readonly root='/opt/muse-domestic-bridge'
readonly config='/etc/muse-domestic-bridge'
readonly state='/var/lib/muse-domestic-bridge'
readonly media='/var/lib/muse-domestic-bridge-media'
readonly unit='/etc/systemd/system/muse-bridge.service'
readonly user='muse-bridge'
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
[[ -r /etc/os-release ]] || fail 'Missing /etc/os-release'
# shellcheck disable=SC1091
source /etc/os-release
[[ ${ID:-} == ubuntu && ${VERSION_ID:-} == 24.04 ]] || fail 'Installer supports Ubuntu 24.04'
[[ -d /run/systemd/system ]] || fail 'A running systemd host is required'
for command in python3 systemctl getent useradd groupadd runuser install mktemp tar ln mv chown chmod; do
    command -v "$command" >/dev/null || fail "Missing prerequisite: $command"
done
python3 -c 'import sys,venv,ensurepip; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' || fail 'Python >=3.11 with venv/ensurepip required'
[[ -f "$here/pyproject.toml" && -f "$here/deploy/muse-bridge.service.in" ]] || fail 'Run from a complete source distribution'
for path in "$root" "$root/releases" "$config" "$config/config.json" "$state" "$media" "$unit"; do
    [[ ! -L "$path" ]] || fail "Managed path is a symlink: $path"
done
for path in "$root/code" "$root/venv"; do
    [[ ! -e "$path" || -L "$path" ]] || fail "Expected managed symlink: $path"
done
if [[ -f "$config/config.json" ]]; then
    python3 - "$config/config.json" "$channel" <<'PY'
import json,sys
with open(sys.argv[1]) as stream: value=json.load(stream)
if value.get('channel')!=sys.argv[2]:
    raise SystemExit('Existing installation belongs to another channel; do not reuse its state.')
PY
fi
if ((check_only)); then
    printf 'Prerequisites OK for one channel: %s. No changes made.\n' "$channel"
    exit 0
fi
((EUID == 0)) || fail 'Run this installer with sudo'
if systemctl is-active --quiet muse-bridge.service; then
    fail 'Stop muse-bridge.service explicitly before an upgrade'
fi
if getent passwd "$user" >/dev/null; then
    IFS=: read -r _ _ uid _ _ home shell <<<"$(getent passwd "$user")"
    [[ $uid -gt 0 && $uid -lt 1000 && $home == "$state" && ($shell == /usr/sbin/nologin || $shell == /bin/false) ]] || fail 'Existing service user needs manual review'
else
    getent group "$user" >/dev/null || groupadd --system "$user"
    useradd --system --gid "$user" --home-dir "$state" --no-create-home --shell /usr/sbin/nologin "$user"
fi
install -d -m 0755 -o root -g root "$root" "$root/releases"
install -d -m 0700 -o "$user" -g "$user" "$config" "$state"
media_group=''
media_path=''
if [[ $channel == qq ]]; then
    getent group muse-bridge-media >/dev/null || groupadd --system muse-bridge-media
    install -d -m 2750 -o "$user" -g muse-bridge-media "$media"
    media_group='SupplementaryGroups=muse-bridge-media'
    media_path=$media
fi
release=$(mktemp -d "$root/releases/release-XXXXXXXX")
chmod 0755 "$release"
install -d -m 0755 "$release/code"
# A fixed publication allowlist; no private logs/configuration or Git history.
(cd "$here"; tar --exclude='__pycache__' --exclude='*.pyc' -cf - pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md licenses src) |
    tar --no-same-owner -xf - -C "$release/code"
chown -R root:root "$release/code"
chmod -R u=rwX,go=rX "$release/code"
python3 -m venv "$release/venv"
"$release/venv/bin/python" -m pip install --only-binary=:all: "$release/code[$channel]"
"$release/venv/bin/python" -m pip check
"$release/venv/bin/muse-bridge" --version
"$release/venv/bin/python" - "$channel" <<'PY'
import importlib,sys
c=sys.argv[1]
for module in ('muse_'+c+'_bridge.muse','muse_'+c+'_bridge.'+('feishu' if c=='feishu' else 'runtime')):
    importlib.import_module(module)
PY
if [[ ! -e "$config/config.json" ]]; then
    extra=()
    [[ $channel != qq ]] || extra=(--shared-dir "$media")
    runuser -u "$user" -- "$release/venv/bin/muse-bridge" init "$channel" --directory "$config" --data-dir "$state" "${extra[@]}"
fi
"$release/venv/bin/python" - "$here/deploy/muse-bridge.service.in" "$release/service.unit" "$channel" "$media_group" "$media_path" <<'PY'
from pathlib import Path
import sys
source=Path(sys.argv[1]).read_text()
for key,value in zip(('CHANNEL','MEDIA_GROUP','MEDIA_PATH'),sys.argv[3:]):
    source=source.replace('@@'+key+'@@',value)
Path(sys.argv[2]).write_text(source)
PY
ln -s "$release/code" "$root/code.next"
mv -Tf "$root/code.next" "$root/code"
ln -s "$release/venv" "$root/venv.next"
mv -Tf "$root/venv.next" "$root/venv"
install -m 0644 "$release/service.unit" "$unit"
systemctl daemon-reload
printf '\nInstalled one channel: %s\n' "$channel"
printf 'Configure credentials as service user:\n  sudo -u %s %s/venv/bin/muse-bridge configure %s --config %s/config.json\n' "$user" "$root" "$channel" "$config"
printf 'Service is not started or enabled. Follow docs/%s.md to finish setup.\n' "$channel"
