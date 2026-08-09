"""Protocol constants shared by the Edge client and central control plane."""
from __future__ import annotations


PERSONAL_READONLY_CAPABILITY = "personal.readonly"
DEFAULT_EDGE_CAPABILITIES = frozenset({PERSONAL_READONLY_CAPABILITY})
EDGE_DATA_POLICY_VERSION = "edge-data-v0.1"
EDGE_ALLOWED_DATA_CLASSIFICATIONS = ("public", "synthetic")
