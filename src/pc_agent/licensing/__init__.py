"""Phase 6 offline licensing (signed tokens, trial, feature gating)."""

from .models import (
    ALL_FEATURES,
    FEATURE_CORE_RECOVERY,
    FEATURE_CORE_TASKS,
    FEATURE_DESKTOP,
    FEATURE_DOCUMENTS,
    FEATURE_GUI,
    FEATURE_PROVIDERS,
    FEATURE_TOOL_BUILDER,
    FEATURE_WEB,
    PLAN_FEATURES,
    LicensePayload,
    LicenseVerification,
    issue_token,
    verify_token,
)
from .service import LicenseService, LicenseStatus, machine_fingerprint

__all__ = [
    "ALL_FEATURES",
    "FEATURE_CORE_RECOVERY",
    "FEATURE_CORE_TASKS",
    "FEATURE_DESKTOP",
    "FEATURE_DOCUMENTS",
    "FEATURE_GUI",
    "FEATURE_PROVIDERS",
    "FEATURE_TOOL_BUILDER",
    "FEATURE_WEB",
    "PLAN_FEATURES",
    "LicensePayload",
    "LicenseService",
    "LicenseStatus",
    "LicenseVerification",
    "issue_token",
    "machine_fingerprint",
    "verify_token",
]
