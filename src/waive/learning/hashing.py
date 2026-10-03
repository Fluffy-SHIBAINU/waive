"""One-way identifiers for the learning tables (spec §11: no identifiers in evidence)."""

import hashlib


def case_hash(case_id: str) -> str:
    """16 hex characters of SHA-256: enough to count distinct cases, useless for finding one."""
    return hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
