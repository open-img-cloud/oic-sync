"""Cosign keyless signature verification of index artifacts.

Every artifact is signed by the open-img-cloud build workflows with cosign keyless (Sigstore).
The bundle signs the sha256 of the qcow2, so it is verified against the digest published in the
index, without downloading the image: the platform later checks that what it imports matches
that digest. The expected identity (a regexp on the workflow URI) and OIDC issuer come from
the index `signing` block.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

import httpx

from oic_sync.index import Artifact, Signing

MAX_BUNDLE_BYTES = 1024 * 1024


class SignatureError(Exception):
    """The artifact signature is missing or does not verify."""


class _Verifier(Protocol):
    def verify_artifact(self, input_: Any, bundle: Any, policy: Any) -> None: ...


class RegexpIdentityPolicy:
    """sigstore verification policy: OIDC issuer equality and SAN URI regexp search.

    Same semantics as `cosign verify-blob --certificate-identity-regexp
    --certificate-oidc-issuer` (Go regexp search, unanchored unless the pattern anchors it).
    """

    def __init__(self, identity_regexp: str, oidc_issuer: str) -> None:
        self._identity = re.compile(identity_regexp)
        self._issuer = oidc_issuer
        self.matched_identity: str | None = None

    def verify(self, cert: Any) -> None:
        from sigstore.verify import policy

        # Fulcio writes the issuer in the V2 extension (DER UTF8String) and, for older
        # certificates, in the legacy V1 extension: accept either.
        policy.AnyOf([policy.OIDCIssuerV2(self._issuer), policy.OIDCIssuer(self._issuer)]).verify(
            cert
        )
        try:
            san = cert.extensions.get_extension_for_class(policy.SubjectAlternativeName).value
        except policy.ExtensionNotFound as e:
            raise policy.VerificationError("certificate has no SubjectAlternativeName") from e
        uris = san.get_values_for_type(policy.UniformResourceIdentifier)
        for uri in uris:
            if self._identity.search(uri):
                self.matched_identity = uri
                return
        raise policy.VerificationError(
            f"certificate identity {uris} does not match {self._identity.pattern!r}"
        )


class SignatureVerifier:
    """Fetches the bundle of an artifact and verifies it against the index sha256."""

    def __init__(
        self, client: httpx.Client, signing: Signing, verifier: _Verifier | None = None
    ) -> None:
        self._client = client
        self._signing = signing
        self._verifier = verifier

    def _get_verifier(self) -> _Verifier:
        if self._verifier is None:
            from sigstore.verify import Verifier

            # Fetches the Sigstore public-good trust root through TUF (cached in ~/.cache).
            self._verifier = Verifier.production()
        return self._verifier

    def _fetch_bundle(self, url: str) -> bytes:
        try:
            response = self._client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as e:
            raise SignatureError(f"cannot fetch bundle {url}: {e}") from e
        if len(response.content) > MAX_BUNDLE_BYTES:
            raise SignatureError(f"bundle {url} is unexpectedly large")
        return response.content

    def verify(self, artifact: Artifact) -> str:
        """Verify the artifact signature; return the signer identity. Raise SignatureError."""
        if not artifact.bundle_url:
            raise SignatureError("the index lists no cosign bundle for this artifact")
        raw = self._fetch_bundle(artifact.bundle_url)

        from sigstore.errors import Error as SigstoreError
        from sigstore.hashes import Hashed
        from sigstore.models import Bundle
        from sigstore_models.common.v1 import HashAlgorithm

        try:
            bundle = Bundle.from_json(raw)
        except Exception as e:
            raise SignatureError(f"invalid bundle {artifact.bundle_url}: {e}") from e
        policy = RegexpIdentityPolicy(self._signing.identity_regexp, self._signing.oidc_issuer)
        hashed = Hashed(algorithm=HashAlgorithm.SHA2_256, digest=bytes.fromhex(artifact.sha256))
        try:
            self._get_verifier().verify_artifact(hashed, bundle, policy)
        except SigstoreError as e:
            raise SignatureError(f"signature verification failed: {e}") from e
        except Exception as e:  # fail closed on anything unexpected (trust root, parsing)
            raise SignatureError(f"signature verification error: {e!r}") from e
        return policy.matched_identity or "unknown"
