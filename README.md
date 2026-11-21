<p align="center">
  <a href="https://openimages.cloud">
    <img src="https://openimages.cloud/img/og.png" alt="openimages.cloud: signed, boot-tested cloud images for OpenStack and Proxmox" width="720">
  </a>
</p>

<h1 align="center">oic-sync</h1>

<p align="center">
  <a href="https://github.com/open-img-cloud/oic-sync/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/open-img-cloud/oic-sync/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://openimages.cloud/verify/"><img alt="Verifies Sigstore cosign signatures" src="https://img.shields.io/badge/verifies-Sigstore%20cosign-2459c4"></a>
  <a href="LICENSE"><img alt="License Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-555"></a>
</p>

<p align="center">
  <a href="https://openimages.cloud"><b>Website</b></a> ·
  <a href="https://openimages.cloud/images/">Catalog</a> ·
  <a href="https://images.openimages.cloud/index.json">index.json</a> ·
  <a href="https://github.com/open-img-cloud/docs/blob/main/docs/oic-sync.md">Documentation</a> ·
  <a href="https://github.com/open-img-cloud/oic-sync/issues">Report an issue</a>
</p>

Keep the signed cloud images of [openimages.cloud](https://openimages.cloud) in sync with your
OpenStack Glance or your Proxmox VE node.

`oic-sync` reads the catalog index (`https://images.openimages.cloud/index.json`), verifies the
cosign signature of each image, imports the versions your cloud does not have yet, gives the
newest one a stable name ("Debian 13" in Glance, `debian-13` on Proxmox VE) and retires the older
ones. It keeps no local state and
can run every day from a systemd timer, a Kubernetes CronJob or a CI job: when everything is up
to date, it changes nothing.

Status: alpha. OpenStack and Proxmox VE (VM templates, tested on Proxmox VE 9.2) are supported,
see [Proxmox VE](#proxmox-ve). Internal and air-gapped clouds can use a local copy of the catalog,
see [Mirror](#mirror).

## Install

```sh
# OpenStack: the backend needs openstacksdk, an optional extra
pipx install 'oic-sync[openstack]'
# or the container image (signed with cosign, OpenStack backend included)
docker run --rm -v ~/.config/openstack:/etc/openstack:ro \
  ghcr.io/open-img-cloud/oic-sync openstack status --cloud mycloud
```

Python 3.10 or newer. Credentials come from the standard
[`clouds.yaml`](https://docs.openstack.org/openstacksdk/latest/user/config/configuration.html)
(`--cloud`), or from `OS_*` environment variables when no cloud is named. An application
credential restricted to the image service (plus read access to servers and volumes for
`--delete`) is enough.

Proxmox VE: `pipx install oic-sync` on the node itself, see [Proxmox VE](#proxmox-ve).

Packages, with systemd services and timers (installed disabled), are attached to each
[GitHub release](https://github.com/open-img-cloud/oic-sync/releases) and signed with cosign:

| System | Package | Options file |
|---|---|---|
| Debian 13, Proxmox VE 9 | `oic-sync_<version>-1.deb13_amd64.deb` | `/etc/default/oic-sync` |
| Debian 12, Proxmox VE 8 | `oic-sync_<version>-1.deb12_amd64.deb` | `/etc/default/oic-sync` |
| EL10 (Rocky, AlmaLinux, RHEL 10) | `oic-sync-<version>-1.el10.x86_64.rpm` | `/etc/sysconfig/oic-sync` |
| EL9 (Rocky, AlmaLinux, RHEL 9; uses python3.12) | `oic-sync-<version>-1.el9.x86_64.rpm` | `/etc/sysconfig/oic-sync` |

```sh
# Proxmox VE 9 node
apt install ./oic-sync_0.2.2-1.deb13_amd64.deb
# set OIC_SYNC_PROXMOX_ARGS in /etc/default/oic-sync, then
systemctl enable --now oic-sync-proxmox.timer
```

Each package holds oic-sync and its Python dependencies in `/opt/oic-sync` and only needs the
system Python.

## Usage

```sh
# What the index offers (no cloud needed)
oic-sync list
# Verify the cosign signatures of the index (no cloud needed)
oic-sync verify --image debian-13
# Copy the catalog to your own storage, for an internal or air-gapped cloud (see Mirror)
oic-sync mirror --dest /srv/oic-mirror --base-url https://mirror.example.net/oic

# Compare a cloud with the index (read only)
oic-sync openstack status --cloud mycloud
# Show what a sync would do (read only, signatures verified)
oic-sync openstack sync --cloud mycloud --image debian-13 --image ubuntu-24.04 --dry-run
# Do it
oic-sync openstack sync --cloud mycloud --image debian-13 --image ubuntu-24.04 --keep 2
# Operator: public images, and remove versions beyond --keep that nothing uses
oic-sync openstack sync --cloud admin --visibility public --keep 3 --delete
```

| Option | Meaning |
|---|---|
| `--image ID` | image to handle, repeatable (default: every image of the index except the service images `octavia-amphora`, `manila-service-image` and `trove-guest-image` the test image `cirros` and the rescue image `cloud-rescue`) |
| `--variant NAME` | only this variant of multi-variant images (`--variant uefi` for Alpine) |
| `--keep N` | versions kept per image, the current one included (default 2) |
| `--delete` | delete versions beyond `--keep`, only when no instance and no volume uses them |
| `--visibility` | `private` (default), `shared`, `community` or `public` (operators) |
| `--dry-run` | read the cloud and verify signatures, change nothing |
| `--import-method` | `auto` (default), `web-download`, `glance-direct` or `upload` |
| `--property K=V` | extra or overriding Glance property (the `oic_*` keys are reserved) |
| `--with-optional-properties` | add `hw_rng_model=virtio` (flavors must allow `hw_rng:allowed`) |
| `--no-verify-signature` | skip the cosign check (not recommended) |
| `--index-url URL` | another index (`$OIC_INDEX_URL`), e.g. a [mirror](#mirror) |
| `--eol` | images whose upstream release is past its end of life ([endoflife.date](https://endoflife.date) dates published in the index): `warn` (default) and handle them, `skip` them, or `fail` before doing anything. Within six months of the end of life it only warns |
| `--cloud`, `--region` | clouds.yaml entry and region (`$OS_CLOUD`) |
| `--timeout` | seconds to wait for an import to finish (default 3600) |

`openstack status --check` exits with 1 when an image is not up to date, for monitoring.
Exit codes: 0 success, 1 at least one image failed (or `--eol fail` refused an image), 2 invalid
arguments or index. `list` marks images `[EOL]` or `[EOL soon]`; `list --json` and
`status --json` carry the `support` dates.

## How it works

For each selected artifact of the index (an image, in one variant):

1. **Find.** Our images are found in Glance by their properties: `oic_id` (and `oic_variant`
   for multi-variant images), owned by the current project. `oic_version` and `oic_sha256`
   tell which version each holds. Nothing else is touched.
2. **Verify.** When the index sha256 is not present yet, the cosign bundle of the artifact is
   verified against that digest, with the signer identity and OIDC issuer published in the
   index (`signing` block): the image must have been signed by an open-img-cloud build
   workflow through GitHub Actions. The image itself is not downloaded for this. A failed
   verification means no import.
3. **Import.** The image is created hidden (`os_hidden=true`) under a versioned name, with all
   the Glance properties of the index (`os_distro`, `hw_disk_bus`, `hw_firmware_type`...),
   `min_disk` from the virtual size, and the `oic_*` keys. Then:
   - if Glance offers the `web-download` import method (`/v2/info/import`), Glance downloads
     the image itself; the multihash it computes (`os_hash_algo`/`os_hash_value`, sha512 by
     default) must equal the index sha512;
   - otherwise the image is streamed from the CDN to Glance (`PUT /file`), with sha256, sha512
     and size computed on the fly; they must equal the index, and Glance's own multihash is
     checked too. Nothing is written to disk.

   On any mismatch or failure, the new image is deleted.
4. **Promote.** The verified image takes the stable name (the image title, "Debian 13", or
   "Alpine Linux (UEFI)" for a variant) and is unhidden.
5. **Retire.** Older versions are renamed "Debian 13 (13-20261026-2643)" and hidden: they
   leave image lists but stay usable by ID, so existing servers, rebuilds and Terraform states
   keep working.
6. **Rotate.** The `--keep` newest versions are kept. With `--delete`, older ones are deleted
   when no server (Nova, `image` filter) and no volume (Cinder, `glance_metadata`) uses them;
   when this cannot be determined (no admin view of other projects for a public image), they
   are kept.

Failed or abandoned imports of a previous run (status `killed`, or still queued after 6 hours)
are removed. A run is idempotent: run it twice, the second changes nothing.

### Naming and infrastructure as code

The stable name moves to each new version. A Terraform or OpenTofu data source that looks an
image up by name will therefore pick the new image after a sync, which can plan the
replacement of existing servers. Look images up by property instead, and ignore image changes
on long-lived servers:

```hcl
data "openstack_images_image_v2" "debian" {
  properties  = { oic_id = "debian-13" }
  most_recent = true
}
```

### Trust chain

The signer is built into oic-sync, not read from the index: a signature is accepted only from
the openimages.cloud build workflows (`^https://github.com/open-img-cloud/\.github/\.github/workflows/build-(libguestfs|dib|nix-flake|freebsd)-image\.yml@`,
issuer `https://token.actions.githubusercontent.com`). The `signing` block of the index is only
compared with it, and a different value is reported as a warning. A tampered index (a compromised
CDN, a mirror served over plain HTTP) therefore cannot vouch for images signed by someone else.
Forks and private catalogs set `OIC_SIGNER_IDENTITY_REGEXP` and `OIC_SIGNER_OIDC_ISSUER`.

The cosign signature covers the sha256 of the qcow2. The streaming upload checks that digest
directly. With `web-download`, Glance only computes one digest (sha512 by default), which is
compared with the sha512 of the index: that value comes from the build MANIFEST, fetched over
HTTPS with the rest of the index, but is not itself covered by the signature. Use
`--import-method upload` if you want the signed digest checked on every byte.

## Proxmox VE

`oic-sync proxmox` keeps one VM template per image version on a Proxmox VE node, ready for
`qm clone`. It runs on the node, as root (like `pveam`), and talks to the local API through
`pvesh`: no API token, nothing to configure.

```sh
# On the node (Proxmox VE 9; 8.x with the "import" content type should work)
apt install pipx
pipx install oic-sync          # no openstacksdk needed
# Downloads go to the "import" content of a file storage: enable it once
# (list the content types the storage already has, plus import)
pvesm set local --content iso,vztmpl,backup,import

oic-sync proxmox status
oic-sync proxmox sync --image debian-13 --image ubuntu-24.04 --dry-run
oic-sync proxmox sync --image debian-13 --image ubuntu-24.04 --vmid-base 9000
# Remove versions beyond --keep that no linked clone uses
oic-sync proxmox sync --image debian-13 --keep 2 --delete

# Use a template
qm clone 9000 120 --name web1 --full 0
qm set 120 --sshkeys ~/.ssh/id_ed25519.pub
qm resize 120 scsi0 +20G && qm start 120
```

`--image`, `--variant`, `--keep`, `--delete`, `--dry-run`, `--timeout`, `--index-url` and
`--no-verify-signature` work as for OpenStack, and `status --check`/`--json` too. Proxmox
options:

| Option | Meaning |
|---|---|
| `--node NAME` | node to work on (default: the node named like this host) |
| `--import-storage` | file storage with the `import` content type, for downloads (default `local`) |
| `--disk-storage` | storage of the template disks, EFI vars and cloud-init drive (default `local-lvm`) |
| `--bridge` | bridge of `net0` (default `vmbr0`) |
| `--vmid-base N` | first VMID for templates, e.g. 9000 (default: the next free VMID of the cluster) |
| `--memory MIB`, `--cores N` | template hardware (default 2048 MiB, 2 cores) |

### What a template looks like

The settings are those of the `qm` commands on each image page of the website:

- `virtio-scsi-single` controller, `scsi0` imported from the qcow2 (`discard=on`), boot from
  `scsi0`, `virtio` network on the bridge, serial console (`serial0: socket`, `vga: serial0`);
- cloud-init drive on `ide2`, `ciuser` set to the image login user, `ipconfig0: ip=dhcp`;
- QEMU guest agent enabled, except for images that do not ship it
  (`hw_qemu_guest_agent=no` in the index, e.g. CirrOS);
- UEFI images: `bios: ovmf`, `machine: q35` and an `efidisk0` (4m, no pre-enrolled keys);
- tags `oic` and `oic-<image id>`, and a description with a machine-readable block that
  oic-sync reads back:

  ````
  ```oic-sync
  oic_id=debian-13
  vmid=9000
  version=13-20261111-2659
  sha256=...
  imported_at=2026-11-13T09:00:00Z
  import_file=local:import/oic-9000-debian-13-13-20261111-2659-x86_64.qcow2
  source=https://images.openimages.cloud/...
  ```
  ````

Only VMs with these tags and this block are touched. Add your own tags freely: they are kept.
A clone copies the tags and the description, but its block names another VMID (`vmid=`): clones
are never touched.

### Import, naming and rotation

1. **Verify.** The cosign bundle is verified against the index sha256, as for OpenStack.
2. **Create** the VM shell (not a template yet) with a free VMID.
3. **Download** the qcow2 with `pvesh create /nodes/<node>/storage/<storage>/download-url
   --content import --checksum <sha256> --checksum-algorithm sha256`: Proxmox checks the
   signed digest itself.
4. **Import** the disk (`scsi0: <storage>:0,import-from=<file>`) and add the cloud-init drive.
5. **Convert** to a template, then delete the downloaded file.

On any failure, the VM and the downloaded file are removed. VM names must be DNS names: the
current version is named after the image id (`debian-13`, `alpine-linux-uefi` for a
variant), older versions `<image id>-<version>` (`debian-13-13-20261026-2643`). Proxmox has no
hidden flag: retired versions get the tag `oic-retired`. The name `debian-13` moves to the
newest template at each sync, so automation should look templates up by tag and name at clone
time; VMs already cloned (linked or full) are not affected by a rename.

With `--delete`, a version beyond `--keep` is destroyed only when no VM of the cluster is a
linked clone of it (a disk referencing its `base-<vmid>-disk-N` volume). An import that never
became a template (crashed run) is removed after 6 hours, with its downloaded file.

### Limitations

- One node per run, through the local API (`pvesh`, as root). Run it on each node that needs
  templates on local storage, or once for a shared storage. No remote mode, no API token.
- VMs only (no LXC templates). qcow2 artifacts only.
- Tested on Proxmox VE 9.2 with LVM-thin (the default `local-lvm`) and directory storages:
  BIOS and UEFI templates, linked clones, rotation, interrupted runs, the packaged systemd
  timer. ZFS and Ceph disk storages should work but have not been tried yet: start with
  `--dry-run` and a single `--image`, and report issues.
- Two runs at the same time on one node may import the same version twice: run it from one
  timer only.

Daily timer on the node (as root):

```ini
# /etc/systemd/system/oic-sync-proxmox.service
[Service]
Type=oneshot
ExecStart=/root/.local/bin/oic-sync proxmox sync --image debian-13 --vmid-base 9000 --keep 2
```

```ini
# /etc/systemd/system/oic-sync-proxmox.timer
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
```

## Mirror

`oic-sync mirror` copies the signed catalog to a directory of your own, for clouds that should
not (or cannot) download from `images.openimages.cloud`. Serve the directory over HTTP and
point oic-sync, Glance, Proxmox VE and people at it instead of the CDN.

```sh
# The latest version of two images (--keep 1 is the default)
oic-sync mirror --dest /srv/oic-mirror --base-url https://mirror.example.net/oic \
  --image debian-13 --image ubuntu-24.04
# Every image except the service ones, two versions each, delete what falls out of --keep
oic-sync mirror --dest /srv/oic-mirror --base-url https://mirror.example.net/oic \
  --keep 2 --prune
# What would be downloaded or pruned (signatures are verified, nothing is written)
oic-sync mirror --dest /srv/oic-mirror --base-url https://mirror.example.net/oic --dry-run

# Then, anywhere that reaches the mirror
export OIC_INDEX_URL=https://mirror.example.net/oic/index.json
oic-sync verify --image debian-13
oic-sync openstack sync --cloud mycloud --image debian-13
```

| Option | Meaning |
|---|---|
| `--dest DIR` | mirror directory (created if needed) |
| `--base-url URL` | URL the directory will be served at; written into the mirror `index.json` |
| `--keep N` | versions per image, the latest included (default 1); older ones come from `versions.json` |
| `--prune` | delete mirrored versions beyond `--keep`, only when the kept ones are complete |
| `--dry-run` | verify signatures and local files, show what would change, write nothing |
| `--image`, `--variant`, `--include-service-images`, `--index-url`, `--eol` | as for the other commands |

The directory has the layout of the CDN, so `<base-url>` is a drop-in replacement for
`https://images.openimages.cloud`:

```
/srv/oic-mirror/
  index.json                      the index, URLs rewritten to --base-url, mirrored images only
  debian-13/
    latest.json                   {"version": "13-20261111-2659"}
    versions.json                 the mirrored versions only
    13-20261111-2659/
      debian-13-20261111-2659-x86_64.qcow2
      debian-13-20261111-2659-x86_64.qcow2.bundle     cosign bundle, copied untouched
      debian-13-20261111-2659-x86_64.qcow2.sha256     (.sha512, .sha1, .md5 when published)
      MANIFEST.json  SHA256SUMS  index.html
```

How the copy is kept safe:

- the cosign bundle of each qcow2 is verified against the index sha256, with the signer
  identity of the index, before anything is downloaded;
- each qcow2 is streamed to a hidden `.<file>.part` file, its size, sha256 and sha512 are
  checked against the index (`versions.json` for older versions), and only then is it renamed
  into place; a mismatch deletes the temporary file and fails the image;
- a file already present with the right digests is not downloaded again (its signature is
  verified again), a corrupt one is replaced, and an interrupted download resumes from its
  `.part` file (HTTP range request);
- `index.json`, `versions.json` and `latest.json` are written last, atomically: an image whose
  new version fails keeps its previous entry, and a broken run never publishes a version that
  is not complete;
- bundles and checksums are copied byte for byte: they cover the digests, not the URLs, so
  `oic-sync verify` and `cosign verify-blob` work on the mirror as on the CDN;
- a lock file (`.oic-sync-mirror.lock`) stops two runs on the same directory.

Run it again at any time: it only downloads what changed. Exit codes: 0 every image mirrored,
1 at least one image failed (the others are still updated), 2 invalid arguments or index.

Serve the directory with any web server, e.g. nginx:

```nginx
server {
    listen 443 ssl;
    server_name mirror.example.net;
    # ssl_certificate ...;
    location ~ /\. { deny all; }   # .part files and the lock
    location /oic/ {
        alias /srv/oic-mirror/;
    }
}
```

or Caddy:

```
mirror.example.net {
    handle_path /oic/* {
        root * /srv/oic-mirror
        @hidden path */.*
        respond @hidden 404
        file_server
    }
}
```

Daily refresh with a systemd timer (as a user that owns `/srv/oic-mirror`):

```ini
# /etc/systemd/system/oic-sync-mirror.service
[Service]
Type=oneshot
User=oic-mirror
ExecStart=/usr/local/bin/oic-sync mirror --dest /srv/oic-mirror \
  --base-url https://mirror.example.net/oic --keep 2 --prune
```

```ini
# /etc/systemd/system/oic-sync-mirror.timer
[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
```

or cron: `17 4 * * * oic-mirror oic-sync mirror --dest /srv/oic-mirror --base-url https://mirror.example.net/oic --keep 2 --prune`.

Notes and limitations:

- The mirror host needs the CDN and the Sigstore trust root (`tuf-repo-cdn.sigstore.dev`).
  Clients that verify signatures (`verify`, `openstack sync`, `proxmox sync`) need the Sigstore
  trust root too, or `--no-verify-signature`; they still check the sha256 of every import.
- Clients verify signatures against the signer built into oic-sync, not against the one the
  mirror `index.json` announces (see [Trust chain](#trust-chain)): a tampered or intercepted
  index cannot vouch for foreign images. `http://` base URLs are accepted for internal mirrors;
  HTTPS is still preferable, as an attacker on the path could serve an older signed version.
- The images keep their upstream file names and versions; a mirror can itself be mirrored
  (`--index-url https://mirror.example.net/oic/index.json`).
- To drop an image from the mirror, delete its directory: the next run removes it from
  `index.json`. Images not selected by a run are left as they are.
- Local directories only for now: sync the directory to object storage (`rclone sync`,
  `aws s3 sync`) if you serve it from a bucket. A native S3 target is future work.

## Run it periodically

- systemd: [`examples/systemd/`](examples/systemd/) (service and daily timer)
- Kubernetes: [`examples/kubernetes/cronjob.yaml`](examples/kubernetes/cronjob.yaml)

The index is rebuilt once a day; a daily run is enough.

## Development

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
ruff check . && ruff format --check .
pytest
```

The tests use fakes for Glance, Nova, Cinder, `pvesh`, HTTP and the mirror directory (a
temporary directory): they never talk to a cloud.

Layout:

- `oic_sync/index.py`: index schema `oic-index/v1`, parsing and selection
- `oic_sync/signature.py`: cosign bundle verification (sigstore-python)
- `oic_sync/engine.py`: platform-neutral planning and execution (import, promote, retire, delete)
- `oic_sync/backends/base.py`: the backend interface
- `oic_sync/backends/openstack.py`: Glance, Nova and Cinder calls
- `oic_sync/backends/proxmox.py`: Proxmox VE templates through `pvesh`
- `oic_sync/hashing.py`: streaming hash reader for uploads
- `oic_sync/mirror.py`: `oic-sync mirror`, the catalog copied to a directory

## License

Apache-2.0
