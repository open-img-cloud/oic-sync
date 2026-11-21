#!/usr/bin/env bash
# Builds the oic-sync RPM from the version published on PyPI.
# Runs as root inside a Rocky Linux container (EL9 or EL10):
#
#   docker run --rm -v "$PWD:/src" -w /src rockylinux/rockylinux:9 packaging/rpm/build.sh 0.1.0
#
# Same layout as the .deb: a private virtual environment in /opt/oic-sync (oic-sync with
# the OpenStack extra), /usr/bin/oic-sync, systemd services and timers (disabled) and
# /etc/sysconfig/oic-sync. EL9 ships Python 3.9 by default, too old for oic-sync: the
# package uses python3.12 there; EL10 uses its default python3 (3.12).
set -euo pipefail

OIC_VERSION="${1:?usage: build.sh <version> [outdir]}"
OUT="${2:-dist-rpm}"
HERE="$(cd "$(dirname "$0")" && pwd)"
PREFIX=/opt/oic-sync

. /etc/os-release
EL="${VERSION_ID%%.*}"   # 9, 10
case "$EL" in
  9) PY=python3.12; PYPKG=python3.12 ;;
  *) PY=python3; PYPKG=python3 ;;
esac
dnf -y -q install "$PYPKG" rpm-build ca-certificates >/dev/null
PYMM=$($PY -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')

rm -rf "$PREFIX"
"$PY" -m venv "$PREFIX"
# PyPI's CDN can serve an index without a just-released version for a few minutes on some
# nodes: retry instead of failing the release.
for attempt in $(seq 1 20); do
  if "$PREFIX/bin/pip" install -q --no-cache-dir --disable-pip-version-check \
    "oic-sync[openstack,s3]==$OIC_VERSION"; then
    break
  fi
  [ "$attempt" -lt 20 ] || { echo "oic-sync $OIC_VERSION not installable from PyPI" >&2; exit 1; }
  echo "pip install failed (attempt $attempt), retrying in 30 s" >&2
  sleep 30
done
"$PREFIX/bin/oic-sync" --version | grep -qx "oic-sync $OIC_VERSION"
"$PREFIX/bin/pip" uninstall -q -y pip
find "$PREFIX" -name '__pycache__' -type d -prune -exec rm -rf {} +

TOP=$(mktemp -d)
mkdir -p "$TOP"/{BUILD,RPMS,SOURCES,SPECS,SRPMS} "$TOP/stage"
STAGE="$TOP/stage"
mkdir -p "$STAGE/opt" "$STAGE/usr/bin" "$STAGE/usr/lib/systemd/system" "$STAGE/etc/sysconfig"
cp -a "$PREFIX" "$STAGE/opt/"
ln -s "$PREFIX/bin/oic-sync" "$STAGE/usr/bin/oic-sync"
install -m 0644 "$HERE"/../files/oic-sync-*.service "$HERE"/../files/oic-sync-*.timer \
  "$STAGE/usr/lib/systemd/system/"
install -m 0644 "$HERE/../files/oic-sync.default" "$STAGE/etc/sysconfig/oic-sync"

cat >"$TOP/SPECS/oic-sync.spec" <<EOF
Name:           oic-sync
Version:        $OIC_VERSION
Release:        1.el$EL
Summary:        Keep openimages.cloud cloud images in sync with OpenStack or Proxmox VE
License:        Apache-2.0
URL:            https://openimages.cloud/oic-sync/
Requires:       $PYPKG
Requires:       ca-certificates
AutoReqProv:    no
%global __os_install_post %{nil}
%global debug_package %{nil}

%description
oic-sync imports the signed, boot-tested qcow2 images of openimages.cloud into
OpenStack Glance or as Proxmox VE VM templates, verifies their cosign signature,
gives the newest version a stable name and rotates older ones. Bundled Python
packages under $PREFIX keep their own licenses. The systemd timers
oic-sync-openstack.timer and oic-sync-proxmox.timer are installed disabled: set
the options in /etc/sysconfig/oic-sync, then enable one.

%install
cp -a $STAGE/. %{buildroot}/

%post
systemctl daemon-reload >/dev/null 2>&1 || :

%preun
if [ \$1 -eq 0 ]; then
  systemctl disable --now oic-sync-proxmox.timer oic-sync-openstack.timer >/dev/null 2>&1 || :
fi

%postun
systemctl daemon-reload >/dev/null 2>&1 || :
# Python writes __pycache__ at run time: files rpm does not know about.
if [ \$1 -eq 0 ]; then rm -rf $PREFIX /var/cache/oic-sync; fi

%files
$PREFIX
/usr/bin/oic-sync
/usr/lib/systemd/system/oic-sync-*.service
/usr/lib/systemd/system/oic-sync-*.timer
%config(noreplace) /etc/sysconfig/oic-sync
EOF

rpmbuild -bb --quiet --define "_topdir $TOP" "$TOP/SPECS/oic-sync.spec" >/dev/null
mkdir -p "$OUT"
cp "$TOP"/RPMS/*/oic-sync-*.rpm "$OUT/"
echo "built $(ls "$OUT"/oic-sync-"$OIC_VERSION"-1.el"$EL".*.rpm) (EL$EL, python $PYMM)"
