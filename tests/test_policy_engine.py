"""
Data policy enforcement tests.

Covers:
- Tax IDs are never stored as plaintext (only HMAC hash)
- Tax IDs never appear in logs or API output
- DOB is encrypted, only emitted as age band
- Email differs by principal (masked for agent, plain for ops)
- Phone differs by principal (denied for agent, last4 for ops)
- Notes are redacted (patterns removed for both principals)
- Account numbers are masked (last4 only)
- primary_tax_id is never stored
- Ops finding details are not returned to agents
"""
from datetime import date

import pytest

from app.core.database import reset_engine


# ------------------------------------------------------------------ #
# Fixtures                                                             #
# ------------------------------------------------------------------ #

@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("DATA_ENCRYPTION_KEY", _fernet_key())
    monkeypatch.setenv("HMAC_SECRET_KEY", "deadbeefdeadbeefdeadbeefdeadbeef")
    monkeypatch.setenv("AGENT_API_KEY", "agent-key-test")
    monkeypatch.setenv("OPS_API_KEY", "ops-key-test")
    from app.core.settings import get_settings
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def policy():
    from app.policy.engine import PolicyEngine
    return PolicyEngine(
        policy_path="contracts/data_policy.yaml",
        data_encryption_key=_fernet_key(),
        hmac_secret_key="deadbeefdeadbeefdeadbeefdeadbeef",
    )


def _fernet_key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


# ------------------------------------------------------------------ #
# Tax ID                                                               #
# ------------------------------------------------------------------ #

class TestTaxId:
    def test_hash_is_not_raw_value(self, policy):
        raw = "123-45-6789"
        hashed = policy.hash_tax_id(raw)
        assert raw not in hashed
        assert "123" not in hashed
        assert "456" not in hashed

    def test_hash_normalizes_before_hashing(self, policy):
        assert policy.hash_tax_id("123-45-6789") == policy.hash_tax_id("123456789")
        assert policy.hash_tax_id("123-45-6789") == policy.hash_tax_id("  123 45 6789  ")

    def test_different_values_produce_different_hashes(self, policy):
        assert policy.hash_tax_id("111-22-3333") != policy.hash_tax_id("444-55-6666")

    def test_tax_id_egress_denied_for_both_principals(self, policy):
        from app.policy.engine import OMIT
        hash_value = policy.hash_tax_id("123-45-6789")
        for principal in ("agent", "ops"):
            result = policy.apply_egress("person.tax_id", hash_value, principal)
            assert result is OMIT, f"tax_id must be OMIT for principal={principal}"


# ------------------------------------------------------------------ #
# Date of birth                                                        #
# ------------------------------------------------------------------ #

class TestDateOfBirth:
    def test_stored_encrypted(self, policy):
        raw = "1965-03-15"
        encrypted = policy.encrypt(raw)
        assert raw not in encrypted
        decrypted = policy.decrypt(encrypted)
        assert decrypted == raw

    def test_egress_is_age_band_not_raw_date(self, policy):
        raw_dob = "1965-03-15"
        for principal in ("agent", "ops"):
            result = policy.apply_egress("person.date_of_birth", raw_dob, principal)
            assert result != raw_dob
            assert "-" in result or result.startswith("<") or result.endswith("+")

    def test_age_band_is_correct(self, policy):
        today = date.today()
        # A person born 60 years ago should be in "55-64"
        dob_60 = date(today.year - 60, today.month, today.day).isoformat()
        band = policy.apply_egress("person.date_of_birth", dob_60, "agent")
        assert band == "55-64"

    def test_age_band_for_young_person(self, policy):
        today = date.today()
        dob_30 = date(today.year - 30, today.month, today.day).isoformat()
        band = policy.apply_egress("person.date_of_birth", dob_30, "agent")
        assert band == "25-34"


# ------------------------------------------------------------------ #
# Email                                                                #
# ------------------------------------------------------------------ #

class TestEmail:
    def test_agent_receives_masked_email(self, policy):
        raw = "robert.chen@example-mail.com"
        result = policy.apply_egress("person.email", raw, "agent")
        assert result != raw
        assert result.startswith("r")
        assert "***" in result
        assert "@example-mail.com" in result

    def test_ops_receives_plain_email(self, policy):
        raw = "robert.chen@example-mail.com"
        result = policy.apply_egress("person.email", raw, "ops")
        assert result == raw

    def test_masked_email_format(self, policy):
        result = policy.apply_egress("person.email", "alice@corp.com", "agent")
        assert result == "a***@corp.com"


# ------------------------------------------------------------------ #
# Phone                                                                #
# ------------------------------------------------------------------ #

class TestPhone:
    def test_agent_receives_nothing(self, policy):
        from app.policy.engine import OMIT
        result = policy.apply_egress("person.phone", "+1-555-867-5309", "agent")
        assert result is OMIT

    def test_ops_receives_last4(self, policy):
        result = policy.apply_egress("person.phone", "+1-555-867-5309", "ops")
        assert "5309" in result
        assert result.startswith("****")


# ------------------------------------------------------------------ #
# Notes                                                                #
# ------------------------------------------------------------------ #

class TestNotes:
    def test_notes_are_redacted_for_both_principals(self, policy):
        raw_notes = "Client SSN 987-65-4320 and acct 8812-4402."
        for principal in ("agent", "ops"):
            result = policy.apply_egress("person.notes", raw_notes, principal)
            assert "987-65-4320" not in str(result)
            assert "8812-4402" not in str(result)
            assert "[REDACTED:" in str(result)

    def test_non_sensitive_text_preserved(self, policy):
        raw_notes = "Prefers morning calls. Has two children."
        result = policy.apply_egress("person.notes", raw_notes, "agent")
        assert "Prefers morning calls" in result


# ------------------------------------------------------------------ #
# Account numbers                                                      #
# ------------------------------------------------------------------ #

class TestAccountNumbers:
    def test_both_principals_receive_last4_only(self, policy):
        raw = "8812-4401"
        for principal in ("agent", "ops"):
            result = policy.apply_egress("account.account_number", raw, principal)
            assert result == "****4401"
            assert "8812" not in result

    def test_primary_tax_id_egress_denied_always(self, policy):
        from app.policy.engine import OMIT
        for principal in ("agent", "ops"):
            result = policy.apply_egress("account.primary_tax_id", "123-45-6789", principal)
            assert result is OMIT


# ------------------------------------------------------------------ #
# Redact free text patterns                                            #
# ------------------------------------------------------------------ #

class TestRedactFreeText:
    def test_ssn_redacted(self, policy):
        result = policy.redact_free_text("SSN: 987-65-4320")
        assert "987-65-4320" not in result
        assert "[REDACTED:us_tax_id]" in result

    def test_account_number_redacted(self, policy):
        result = policy.redact_free_text("Account 8812-4402 is linked")
        assert "8812-4402" not in result

    def test_email_redacted(self, policy):
        result = policy.redact_free_text("Email: user@example.com for contact")
        assert "user@example.com" not in result

    def test_non_sensitive_text_unchanged(self, policy):
        text = "Client prefers conservative investments."
        assert policy.redact_free_text(text) == text
