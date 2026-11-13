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
OpenStack Glance.

`oic-sync` reads the catalog index (`https://images.openimages.cloud/index.json`), verifies the
cosign signature of each image, imports the versions your cloud does not have yet, gives the
newest one a stable name ("Debian 13") and retires the older ones. It keeps no local state and
can run every day from a systemd timer, a Kubernetes CronJob or a CI job: when everything is up
to date, it changes nothing.

Status: alpha. OpenStack is supported; Proxmox VE is planned.

## Install

```sh
pipx install oic-sync
# or the container image (signed with cosign)
docker run --rm -v ~/.config/openstack:/etc/openstack:ro \
  ghcr.io/open-img-cloud/oic-sync openstack status --cloud mycloud
```

Python 3.10 or newer. Credentials come from the standard
[`clouds.yaml`](https://docs.openstack.org/openstacksdk/latest/user/config/configuration.html)
(`--cloud`), or from `OS_*` environment variables when no cloud is named. An application
credential restricted to the image service (plus read access to servers and volumes for
`--delete`) is enough.

## Usage

```sh
# What the index offers (no cloud needed)
oic-sync list
# Verify the cosign signatures of the index (no cloud needed)
oic-sync verify --image debian-13

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
| `--image ID` | image to handle, repeatable (default: every image of the index except the service images `octavia-amphora`, `manila-service-image` and `trove-guest-image` and the test image `cirros`) |
| `--variant NAME` | only this variant of multi-variant images (`--variant uefi` for Alpine) |
| `--keep N` | versions kept per image, the current one included (default 2) |
| `--delete` | delete versions beyond `--keep`, only when no instance and no volume uses them |
| `--visibility` | `private` (default), `shared`, `community` or `public` (operators) |
| `--dry-run` | read the cloud and verify signatures, change nothing |
| `--import-method` | `auto` (default), `web-download`, `glance-direct` or `upload` |
| `--property K=V` | extra or overriding Glance property (the `oic_*` keys are reserved) |
| `--with-optional-properties` | add `hw_rng_model=virtio` (flavors must allow `hw_rng:allowed`) |
| `--no-verify-signature` | skip the cosign check (not recommended) |
| `--index-url URL` | another index (`$OIC_INDEX_URL`), e.g. a mirror |
| `--cloud`, `--region` | clouds.yaml entry and region (`$OS_CLOUD`) |

`openstack status --check` exits with 1 when an image is not up to date, for monitoring.
Exit codes: 0 success, 1 at least one image failed, 2 invalid arguments or index.

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

The cosign signature covers the sha256 of the qcow2. The streaming upload checks that digest
directly. With `web-download`, Glance only computes one digest (sha512 by default), which is
compared with the sha512 of the index: that value comes from the build MANIFEST, fetched over
HTTPS with the rest of the index, but is not itself covered by the signature. Use
`--import-method upload` if you want the signed digest checked on every byte.

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

The tests use fakes for Glance, Nova, Cinder and HTTP: they never talk to a cloud.

Layout:

- `oic_sync/index.py`: index schema `oic-index/v1`, parsing and selection
- `oic_sync/signature.py`: cosign bundle verification (sigstore-python)
- `oic_sync/engine.py`: platform-neutral planning and execution (import, promote, retire, delete)
- `oic_sync/backends/base.py`: the backend interface (Proxmox VE will implement it)
- `oic_sync/backends/openstack.py`: Glance, Nova and Cinder calls
- `oic_sync/hashing.py`: streaming hash reader for uploads

## License

Apache-2.0
