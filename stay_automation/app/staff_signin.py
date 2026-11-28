"""Issue staff sign-in assertions only from authenticated Home Assistant ingress."""
import base64
import json
import secrets
import time

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

RENDER_ORIGIN = "https://atlanta-temporary-housing-extension-pilot.onrender.com"
STAFF = frozenset({"metroareahomes@gmail.com", "mike@atlantahomeinvesting.com"})
DESTINATIONS = frozenset({"owner", "pilot"})


def staff_username(request):
    if not request.client or request.client.host != "172.30.32.2":
        return None
    username = request.headers.get("x-remote-user-name", "").casefold()
    if username in STAFF and request.headers.get("x-remote-user-id"):
        return username
    return None


def signing_key(db):
    saved = db.get_setting("staff_signin_private_key")
    if not saved:
        saved = secrets.token_hex(32)
        db.set_setting("staff_signin_private_key", saved)
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(saved))


def public_key(db):
    return base64.b64encode(signing_key(db).public_key().public_bytes(
        Encoding.DER, PublicFormat.SubjectPublicKeyInfo
    )).decode()


def assertion(db, username, destination):
    claims = {
        "aud": RENDER_ORIGIN, "sub": username, "destination": destination,
        "exp": int(time.time()) + 30, "nonce": secrets.token_urlsafe(24),
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=")
    signature = base64.urlsafe_b64encode(signing_key(db).sign(payload)).rstrip(b"=")
    return (payload + b"." + signature).decode()
