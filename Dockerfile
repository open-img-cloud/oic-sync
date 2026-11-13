# syntax=docker/dockerfile:1
# OCI image of oic-sync: ghcr.io/open-img-cloud/oic-sync (signed with cosign keyless).
# It ships the OpenStack backend; the Proxmox VE backend runs on the node itself (pipx).
#
#   docker run --rm -v ~/.config/openstack:/etc/openstack:ro \
#     ghcr.io/open-img-cloud/oic-sync openstack sync --cloud mycloud --dry-run

ARG PYTHON_IMAGE=docker.io/library/python:3.13-slim

FROM ${PYTHON_IMAGE} AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /dist .

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.title="oic-sync" \
      org.opencontainers.image.description="Keep openimages.cloud signed cloud images in sync with OpenStack Glance" \
      org.opencontainers.image.source="https://github.com/open-img-cloud/oic-sync" \
      org.opencontainers.image.licenses="Apache-2.0"
RUN --mount=type=bind,from=build,source=/dist,target=/dist \
    pip install --no-cache-dir "$(echo /dist/*.whl)[openstack]" \
 && useradd --no-log-init --create-home --home-dir /home/oic --uid 10001 oic
# clouds.yaml is looked up in /etc/openstack (mount it read only) or ~/.config/openstack.
# The Sigstore trust root is cached under ~/.cache/sigstore (writable home).
USER 10001
ENV HOME=/home/oic PYTHONUNBUFFERED=1
ENTRYPOINT ["oic-sync"]
CMD ["--help"]
