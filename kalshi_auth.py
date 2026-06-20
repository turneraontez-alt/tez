"""RSA-PSS signing for Kalshi API v2 authenticated requests.

Signing formula:  timestamp_ms_str + METHOD.upper() + path (NO query params)
Required headers: KALSHI-ACCESS-KEY, KALSHI-ACCESS-SIGNATURE (b64), KALSHI-ACCESS-TIMESTAMP
WS sign path:     /trade-api/ws/v2

Credentials are read from environment secrets only and must never be logged,
returned to the frontend, or included in any API response.
"""
import base64
import logging
import os
import time

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

logger = logging.getLogger(__name__)

WS_SIGN_PATH = "/trade-api/ws/v2"

# Known PEM type markers we accept
_PEM_TYPES = ["RSA PRIVATE KEY", "PRIVATE KEY", "EC PRIVATE KEY"]


def _normalize_pem(raw: str) -> str:
    """Return a properly-formatted PEM string regardless of how the secret was stored.

    Handles all common storage artifacts:
    - Already correct (passthrough)
    - Escaped newlines (\\n → real newline)
    - Leading/trailing dashes missing from the header or footer marker
    - Header/footer stripped entirely (raw base64 body only, spaces as separators)
    - Header present but spaces used instead of newlines throughout
    - PKCS#8 "PRIVATE KEY" header variants
    """
    import re as _re
    import base64 as _b64

    s = raw.strip()

    # 1. Resolve escaped newlines first
    if "\\n" in s and "\n" not in s:
        s = s.replace("\\n", "\n")

    # 2. If it already has proper -----BEGIN header and real newlines, return as-is
    if s.startswith("-----BEGIN") and "\n" in s:
        if not s.endswith("\n"):
            s += "\n"
        return s

    # 3. Detect key type from any header fragment present in the string
    pem_type = "RSA PRIVATE KEY"  # default
    for pt in _PEM_TYPES:
        if pt in s:
            pem_type = pt
            break

    # 4. Strip ALL header/footer fragments, with or without their surrounding
    #    dashes (the BEGIN/END marker may be missing leading or trailing dashes,
    #    or use spaces instead of newlines).
    s = _re.sub(r"-*\s*BEGIN\s+[A-Z ]+\s*-*", " ", s)
    s = _re.sub(r"-*\s*END\s+[A-Z ]+\s*-*", " ", s)

    # 5. Now s is just the base64 body (possibly with spaces/newlines as separators)
    body = _re.sub(r"\s+", "", s)  # strip all whitespace

    # 6. Re-detect type from DER if no header was present
    if pem_type == "RSA PRIVATE KEY":
        try:
            der = _b64.b64decode(body + "==")
            # PKCS#8 has an AlgorithmIdentifier (0x30 tag) at byte offset 4
            if len(der) > 5 and der[4] == 0x30:
                pem_type = "PRIVATE KEY"
        except Exception:
            pass

    # 7. Wrap body at 64-char boundaries and reassemble
    wrapped = "\n".join(body[i:i + 64] for i in range(0, len(body), 64))
    return f"-----BEGIN {pem_type}-----\n{wrapped}\n-----END {pem_type}-----\n"


class KalshiSigner:
    """Loads the RSA-4096 private key and produces per-request auth headers.

    Stateless after __init__ — safe to share across threads.
    """

    def __init__(self):
        self.key_id = (os.environ.get("KALSHI_API_KEY_ID") or "").strip()
        pem = os.environ.get("KALSHI_PRIVATE_KEY") or ""
        self._key = None
        self._error: str | None = None

        if not self.key_id:
            self._error = "KALSHI_API_KEY_ID not set"
        elif not pem:
            self._error = "KALSHI_PRIVATE_KEY not set"
        else:
            try:
                pem = _normalize_pem(pem)
                self._key = serialization.load_pem_private_key(
                    pem.encode("utf-8"),
                    password=None,
                    backend=default_backend(),
                )
                logger.info(
                    "Kalshi signer: private key loaded (key_id prefix: %s…)",
                    self.key_id[:8],
                )
            except Exception as exc:
                self._error = f"key load failed — {exc.__class__.__name__}: {exc}"
                logger.error("Kalshi signer: %s", self._error)

    @property
    def available(self) -> bool:
        return self._key is not None and bool(self.key_id)

    @property
    def error(self) -> str | None:
        return self._error

    def sign(self, method: str, path: str) -> dict:
        """Return the three Kalshi auth headers for a request.

        Parameters
        ----------
        method : str  HTTP method, e.g. "GET"
        path   : str  Path WITHOUT query params, e.g. "/trade-api/ws/v2"
        """
        if not self.available:
            raise RuntimeError(f"Signer not available: {self._error}")
        ts = str(int(time.time() * 1000))
        message = (ts + method.upper() + path).encode("utf-8")
        sig_bytes = self._key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig_bytes).decode("utf-8"),
            "KALSHI-ACCESS-TIMESTAMP": ts,
        }

    def ws_headers(self) -> list[str]:
        """Auth headers formatted as 'Key: Value' strings for websocket-client."""
        h = self.sign("GET", WS_SIGN_PATH)
        return [f"{k}: {v}" for k, v in h.items()]
