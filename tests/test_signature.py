from __future__ import annotations

import base64
import datetime
import os

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

from fakes import IDENTITY_RE, ISSUER
from oic_sync.http import make_client
from oic_sync.index import Artifact, Signing
from oic_sync.signature import RegexpIdentityPolicy, SignatureError, SignatureVerifier

# A real artifact of the catalog, signed by the build workflow (used by the live tests).
REAL_URL = "https://images.openimages.cloud/fedora-44/44-1.7/fedora-44-1.7-x86_64.qcow2"
REAL_SHA256 = "1984dc5eb5fe7799055e3c2f08c726f0795fea7dbc6464550016108e77315907"
WORKFLOW = (
    "https://github.com/open-img-cloud/.github/.github/workflows/"
    "build-libguestfs-image.yml@refs/heads/main"
)

ISSUER_V1 = ObjectIdentifier("1.3.6.1.4.1.57264.1.1")
ISSUER_V2 = ObjectIdentifier("1.3.6.1.4.1.57264.1.8")


def der_utf8(value: str) -> bytes:
    raw = value.encode()
    assert len(raw) < 128
    return bytes([0x0C, len(raw)]) + raw


def make_cert(san_uri: str | None, issuer: str | None, *, v2: bool = True) -> x509.Certificate:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(minutes=10))
    )
    if san_uri:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(san_uri)]), False
        )
    if issuer:
        if v2:
            ext = x509.UnrecognizedExtension(ISSUER_V2, der_utf8(issuer))
        else:
            ext = x509.UnrecognizedExtension(ISSUER_V1, issuer.encode())
        builder = builder.add_extension(ext, False)
    return builder.sign(key, hashes.SHA256())


def artifact(sha256: str = REAL_SHA256, bundle_url: str | None = REAL_URL + ".bundle"):
    return Artifact(
        variant=None,
        version="44-1.7",
        arch="x86_64",
        format="qcow2",
        firmware="bios",
        url=REAL_URL,
        sha256=sha256,
        sha512=None,
        size=None,
        virtual_size=None,
        bundle_url=bundle_url,
        manifest_url=None,
        signed=True,
        built_at=None,
    )


class TestPolicy:
    def test_accepts_matching_identity_v2_issuer(self):
        policy = RegexpIdentityPolicy(IDENTITY_RE, ISSUER)
        policy.verify(make_cert(WORKFLOW, ISSUER))
        assert policy.matched_identity == WORKFLOW

    def test_accepts_legacy_v1_issuer(self):
        RegexpIdentityPolicy(IDENTITY_RE, ISSUER).verify(make_cert(WORKFLOW, ISSUER, v2=False))

    @pytest.mark.parametrize(
        "uri",
        [
            # another org
            "https://github.com/evil/.github/.github/workflows/build-libguestfs-image.yml@x",
            # a workflow of an image repository, not the shared build workflow
            "https://github.com/open-img-cloud/debian-13/.github/workflows/release.yml@x",
            # the dot must be literal
            "https://github.com/open-img-cloud/x.github/.github/workflows/build-dib-image.yml@x",
        ],
    )
    def test_rejects_other_identities(self, uri):
        from sigstore.errors import VerificationError

        with pytest.raises(VerificationError, match="does not match"):
            RegexpIdentityPolicy(IDENTITY_RE, ISSUER).verify(make_cert(uri, ISSUER))

    def test_rejects_other_issuer(self):
        from sigstore.errors import VerificationError

        with pytest.raises(VerificationError):
            RegexpIdentityPolicy(IDENTITY_RE, ISSUER).verify(
                make_cert(WORKFLOW, "https://accounts.google.com")
            )

    def test_rejects_missing_issuer_and_san(self):
        from sigstore.errors import VerificationError

        with pytest.raises(VerificationError):
            RegexpIdentityPolicy(IDENTITY_RE, ISSUER).verify(make_cert(WORKFLOW, None))
        with pytest.raises(VerificationError):
            RegexpIdentityPolicy(IDENTITY_RE, ISSUER).verify(make_cert(None, ISSUER))


class RecordingVerifier:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.seen = []

    def verify_artifact(self, input_, bundle, policy):
        self.seen.append((input_, bundle, policy))
        if self.error:
            raise self.error
        policy.matched_identity = WORKFLOW


FAKE_BUNDLE = b'{"mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json"}'


@pytest.fixture(autouse=True)
def fake_bundle_parser(request, monkeypatch):
    """Offline tests: any bundle except obvious garbage parses into a placeholder."""
    if request.node.get_closest_marker("live"):
        return
    from sigstore.models import Bundle

    real = Bundle.from_json

    def from_json(raw):
        if raw == FAKE_BUNDLE:
            return object()
        return real(raw)

    monkeypatch.setattr(Bundle, "from_json", staticmethod(from_json))


def bundle_client(status: int = 200, content: bytes | None = None) -> httpx.Client:
    body = FAKE_BUNDLE if content is None else content

    def handler(request):
        return httpx.Response(status, content=body)

    return make_client(transport=httpx.MockTransport(handler))


SIGNING = Signing(IDENTITY_RE, ISSUER)


def test_verifier_checks_the_index_digest_not_a_download():
    inner = RecordingVerifier()
    with bundle_client() as c:
        identity = SignatureVerifier(c, SIGNING, inner).verify(artifact())
    assert identity == WORKFLOW
    hashed, _bundle, policy = inner.seen[0]
    assert hashed.digest == bytes.fromhex(REAL_SHA256)
    assert hashed.algorithm.name == "SHA2_256"
    assert isinstance(policy, RegexpIdentityPolicy)


def test_verifier_failure_is_a_signature_error():
    from sigstore.errors import VerificationError

    inner = RecordingVerifier(VerificationError("Bundle message digest mismatch"))
    with bundle_client() as c, pytest.raises(SignatureError, match="digest mismatch"):
        SignatureVerifier(c, SIGNING, inner).verify(artifact())


def test_unexpected_errors_fail_closed():
    inner = RecordingVerifier(RuntimeError("TUF unreachable"))
    with bundle_client() as c, pytest.raises(SignatureError, match="TUF"):
        SignatureVerifier(c, SIGNING, inner).verify(artifact())


def test_missing_bundle_url():
    with bundle_client() as c, pytest.raises(SignatureError, match="no cosign bundle"):
        SignatureVerifier(c, SIGNING, RecordingVerifier()).verify(artifact(bundle_url=None))


def test_bundle_not_found():
    with bundle_client(404) as c, pytest.raises(SignatureError, match="cannot fetch"):
        SignatureVerifier(c, SIGNING, RecordingVerifier()).verify(artifact())


def test_invalid_bundle():
    with bundle_client(content=b'{"x": 1}') as c, pytest.raises(SignatureError, match="invalid"):
        SignatureVerifier(c, SIGNING, RecordingVerifier()).verify(artifact())


def live_bundle() -> tuple[bytes, str]:
    """A real bundle from the CDN and the digest it signs.

    The digest is read from the bundle itself, so the test does not depend on the index
    being in step with the CDN.
    """
    with make_client() as c:
        response = c.get(REAL_URL + ".bundle")
        response.raise_for_status()
    raw = response.content
    import json

    digest = json.loads(raw)["messageSignature"]["messageDigest"]["digest"]
    return raw, base64.b64decode(digest).hex()


@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("OIC_LIVE_TESTS"), reason="set OIC_LIVE_TESTS=1 (network: CDN, Sigstore)"
)
class TestLive:
    """Real Sigstore verification of a real catalog bundle (needs the network)."""

    def test_real_bundle_verifies(self):
        raw, digest = live_bundle()
        with bundle_client(content=raw) as c:
            assert SignatureVerifier(c, SIGNING).verify(artifact(digest)) == WORKFLOW

    def test_real_bundle_rejects_other_digest(self):
        raw, _ = live_bundle()
        with bundle_client(content=raw) as c, pytest.raises(SignatureError, match="mismatch"):
            SignatureVerifier(c, SIGNING).verify(artifact(sha256="0" * 64))

    def test_real_bundle_rejects_other_identity(self):
        raw, digest = live_bundle()
        signing = Signing(r"^https://github\.com/open-img-cloud/oic-sync/", ISSUER)
        with bundle_client(content=raw) as c, pytest.raises(SignatureError):
            SignatureVerifier(c, signing).verify(artifact(digest))
