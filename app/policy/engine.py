"""
Policy engine — loads contracts/data_policy.yaml and enforces field-level rules
at store time and API egress. All field-specific logic lives here, not in route handlers.
"""
import hashlib
import hmac
import re
from datetime import date
from typing import Any

import yaml
from cryptography.fernet import Fernet

# Sentinel returned by apply_egress when a field must be omitted entirely from the response.
OMIT = object()


class PolicyEngine:
    def __init__(self, policy_path: str, data_encryption_key: str, hmac_secret_key: str) -> None:
        with open(policy_path, "r", encoding="utf-8") as f:
            self._policy: dict = yaml.safe_load(f)

        self._fernet = Fernet(data_encryption_key.encode()
                              if not isinstance(data_encryption_key, bytes)
                              else data_encryption_key)
        self._hmac_key: bytes = (
            hmac_secret_key.encode()
            if not isinstance(hmac_secret_key, bytes)
            else hmac_secret_key
        )
        self._redact_patterns: dict[str, re.Pattern] = self._compile_redact_patterns()

    # ------------------------------------------------------------------ store

    def hash_tax_id(self, raw: str) -> str:
        """Keyed HMAC-SHA256 of the normalised tax ID. Raw value is never retained."""
        normalized = re.sub(r"[\s\-]", "", raw.strip())
        return hmac.new(self._hmac_key, normalized.encode(), hashlib.sha256).hexdigest()

    def encrypt(self, value: str) -> str:
        """Fernet-encrypt a string. Returns a URL-safe base64 token."""
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Decrypt a Fernet token back to the original string."""
        return self._fernet.decrypt(token.encode()).decode()

    def redact_free_text(self, text: str) -> str:
        """Apply all redact_free_text patterns from the policy before storing notes."""
        for name, pattern in self._redact_patterns.items():
            text = pattern.sub(f"[REDACTED:{name}]", text)
        return text

    # ------------------------------------------------------------------ egress

    def apply_egress(self, field_path: str, stored_value: Any, principal: str) -> Any:
        """
        Apply the egress transform for a field + principal combination.
        Returns the transformed value, or OMIT if the field must be excluded.
        stored_value should be the decrypted/plaintext value for encrypted fields.
        """
        field_cfg = self._policy.get("fields", {}).get(field_path, {})
        egress_cfg = field_cfg.get("egress", {})
        transform_name: str = egress_cfg.get(principal, "plain")

        if transform_name == "deny":
            return OMIT
        if transform_name == "plain":
            return stored_value
        return self._apply_named_transform(transform_name, stored_value)

    # ------------------------------------------------------------------ helpers

    def _compile_redact_patterns(self) -> dict[str, re.Pattern]:
        patterns: dict[str, re.Pattern] = {}
        rft = self._policy.get("transforms", {}).get("redact_free_text", {})
        for name, pattern_str in rft.get("patterns", {}).items():
            patterns[name] = re.compile(pattern_str)
        return patterns

    def _apply_named_transform(self, transform_name: str, value: Any) -> Any:
        if transform_name == "age_band":
            return self._compute_age_band(value)
        if transform_name == "mask_email":
            return self._mask_email(str(value))
        if transform_name == "last4":
            return self._last4(str(value))
        if transform_name == "redact_free_text":
            return self.redact_free_text(str(value))
        return value

    def _compute_age_band(self, dob_value: Any) -> str:
        today = date.today()
        if isinstance(dob_value, str):
            dob = date.fromisoformat(dob_value)
        elif isinstance(dob_value, date):
            dob = dob_value
        else:
            return "unknown"
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        bands: list[str] = self._policy["transforms"]["age_band"]["bands"]
        for band in bands:
            if band.startswith("<"):
                if age < int(band[1:]):
                    return band
            elif band.endswith("+"):
                if age >= int(band[:-1]):
                    return band
            else:
                lo, hi = map(int, band.split("-"))
                if lo <= age <= hi:
                    return band
        return bands[-1]

    @staticmethod
    def _mask_email(email: str) -> str:
        email = email.strip()
        if "@" not in email:
            return "***@***"
        local, domain = email.split("@", 1)
        return f"{local[0]}***@{domain}"

    @staticmethod
    def _last4(value: str) -> str:
        digits = re.sub(r"\D", "", value)
        tail = digits[-4:] if len(digits) >= 4 else value[-4:]
        return f"****{tail}"

    # ------------------------------------------------------------------ introspection

    def store_rule(self, field_path: str) -> str:
        return self._policy.get("fields", {}).get(field_path, {}).get("store", "plain")

    def log_rule(self, field_path: str) -> str:
        return self._policy.get("fields", {}).get(field_path, {}).get("log", "allow")

    def principals(self) -> list[str]:
        return list(self._policy.get("principals", {}).keys())

    def principal_scopes(self, principal: str) -> list[str]:
        return self._policy.get("principals", {}).get(principal, {}).get("scopes", [])
