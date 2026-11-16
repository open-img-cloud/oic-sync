#!/usr/bin/env bash
# Builds the oic-sync .deb from the version published on PyPI.
# Runs as root inside a debian:<suite> container (bookworm, trixie):
#
#   docker run --rm -v "$PWD:/src" -w /src debian:trixie packaging/deb/build.sh 0.1.0
#
# The package holds a private virtual environment in /opt/oic-sync (oic-sync with the
# OpenStack extra and its dependencies; Debian does not ship the sigstore versions it
# needs), /usr/bin/oic-sync, systemd services and timers for Proxmox VE and OpenStack
# (disabled), and /etc/default/oic-sync. It depends on the python3 of the suite only.
set -euo pipefail

OIC_VERSION="${1:?usage: build.sh <version> [outdir]}"
OUT="${2:-dist-deb}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/oic-sync

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends python3 python3-venv ca-certificates dpkg-dev >/dev/null

# os-release defines VERSION too: hence OIC_VERSION above.
. /etc/os-release
SUITE="$VERSION_CODENAME"
DEBVER="$VERSION_ID"   # 12, 13
PYMM=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYNEXT=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor + 1}")')
ARCH=$(dpkg --print-architecture)

# The venv is created at its final path: its scripts carry absolute shebangs.
rm -rf "$PREFIX"
python3 -m venv "$PREFIX"
"$PREFIX/bin/pip" install -q --no-cache-dir --disable-pip-version-check \
  "oic-sync[openstack]==$OIC_VERSION"
"$PREFIX/bin/oic-sync" --version | grep -qx "oic-sync $OIC_VERSION"
# Not needed at run time.
"$PREFIX/bin/pip" uninstall -q -y pip
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +

ROOT=$(mktemp -d)
mkdir -p "$ROOT/DEBIAN" "$ROOT/opt" "$ROOT/usr/bin" "$ROOT/lib/systemd/system" \
  "$ROOT/etc/default" "$ROOT/usr/share/doc/oic-sync"
cp -a "$PREFIX" "$ROOT/opt/"
ln -s "$PREFIX/bin/oic-sync" "$ROOT/usr/bin/oic-sync"
install -m 0644 "$HERE"/../files/oic-sync-*.service "$HERE"/../files/oic-sync-*.timer "$ROOT/lib/systemd/system/"
install -m 0644 "$HERE/../files/oic-sync.default" "$ROOT/etc/default/oic-sync"
cat >"$ROOT/usr/share/doc/oic-sync/copyright" <<EOF
oic-sync $OIC_VERSION: https://github.com/open-img-cloud/oic-sync
License: Apache-2.0 (oic-sync). The bundled Python packages under $PREFIX keep their own
licenses (see their dist-info directories).
EOF

SIZE=$(du -sk "$ROOT" | cut -f1)
PKGVER="${OIC_VERSION}-1~deb${DEBVER}"
cat >"$ROOT/DEBIAN/control" <<EOF
Package: oic-sync
Version: $PKGVER
Architecture: $ARCH
Maintainer: Open Image Cloud <security@openimages.cloud>
Installed-Size: $SIZE
Depends: python3 (>= $PYMM), python3 (<< $PYNEXT), ca-certificates
Section: admin
Priority: optional
Homepage: https://openimages.cloud/oic-sync/
Description: keep openimages.cloud cloud images in sync with OpenStack or Proxmox VE
 oic-sync imports the signed, boot-tested qcow2 images of openimages.cloud into
 OpenStack Glance or as Proxmox VE VM templates, verifies their cosign signature,
 gives the newest version a stable name and rotates older ones.
 .
 The systemd timers oic-sync-proxmox.timer and oic-sync-openstack.timer are
 installed disabled: set the options in /etc/default/oic-sync, then enable one.
EOF
echo /etc/default/oic-sync >"$ROOT/DEBIAN/conffiles"
cat >"$ROOT/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then systemctl daemon-reload || true; fi
EOF
cat >"$ROOT/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then systemctl daemon-reload || true; fi
# Python writes __pycache__ at run time: files dpkg does not know about.
if [ "$1" = remove ] || [ "$1" = purge ]; then rm -rf /opt/oic-sync; fi
if [ "$1" = purge ]; then rm -rf /var/cache/oic-sync; fi
EOF
cat >"$ROOT/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ -d /run/systemd/system ] && [ "$1" = remove ]; then
  systemctl disable --now oic-sync-proxmox.timer oic-sync-openstack.timer >/dev/null 2>&1 || true
fi
EOF
chmod 0755 "$ROOT/DEBIAN/postinst" "$ROOT/DEBIAN/postrm" "$ROOT/DEBIAN/prerm"

mkdir -p "$OUT"
# GitHub release assets turn "~" into ".": name the file without it (the package version
# keeps ~debNN so that a later Debian upload sorts higher).
DEB="$OUT/oic-sync_${OIC_VERSION}-1.deb${DEBVER}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$ROOT" "$DEB" >/dev/null
echo "built $DEB ($SUITE, python $PYMM)"
