"""Machine identifier used to bind a license to one install. Ported
verbatim (same approach) from SureFramePro's device_fingerprint.py."""

import hashlib
import platform
import uuid


def get_machine_id() -> str:
    raw = platform.node() + platform.system() + str(uuid.getnode())
    return hashlib.sha256(raw.encode()).hexdigest()[:16].upper()
