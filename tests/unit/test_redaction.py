from repopilot.tools.redaction import Redactor


def test_redactor_removes_configured_and_api_key_secrets() -> None:
    """Leaking either known values or key-shaped values would expose credentials."""
    redactor = Redactor(secrets={"actual-secret"})
    value = redactor.redact("OPENAI_API_KEY=sk-test123 actual-secret")
    assert value == "OPENAI_API_KEY=[REDACTED] [REDACTED]"


def test_redactor_walks_nested_structures_without_changing_non_secret_types() -> None:
    """Only redacting top-level logs would leak secrets inside tool result payloads."""
    redactor = Redactor(secrets={"tenant-token"})
    value = redactor.redact(
        {
            "message": "token: tenant-token",
            "items": ["safe", "password=hunter2"],
            "count": 2,
            "ok": True,
        }
    )
    assert value == {
        "message": "token: [REDACTED]",
        "items": ["safe", "password=[REDACTED]"],
        "count": 2,
        "ok": True,
    }


def test_redactor_does_not_modify_safe_text() -> None:
    """Corrupting ordinary logs would make the audit trajectory unusable."""
    redactor = Redactor()
    assert redactor.redact("pytest passed in 0.12s") == "pytest passed in 0.12s"
