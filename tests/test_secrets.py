from devmemory import secrets


def test_redacts_aws_access_key_id():
    text = "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP"
    redacted, count = secrets.redact(text)
    assert count == 1
    assert "AKIAABCDEFGHIJKLMNOP" not in redacted
    assert "[REDACTED:aws_access_key_id]" in redacted


def test_redacts_pem_private_key_block():
    text = (
        "before\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA1234567890abcdef\n"
        "-----END RSA PRIVATE KEY-----\n"
        "after"
    )
    redacted, count = secrets.redact(text)
    assert count == 1
    assert "MIIEpAIBAAKCAQEA1234567890abcdef" not in redacted
    assert "before" in redacted and "after" in redacted


def test_redacts_github_token():
    redacted, count = secrets.redact("token: ghp_" + "a" * 36)
    assert count == 1
    assert "ghp_" + "a" * 36 not in redacted


def test_redacts_slack_token():
    redacted, count = secrets.redact("SLACK_TOKEN=xoxb-123456-abcdef")
    assert count == 1
    assert "xoxb-123456-abcdef" not in redacted


def test_redacts_jwt():
    fake_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    redacted, count = secrets.redact(f"Authorization: Bearer {fake_jwt}")
    assert count == 1
    assert fake_jwt not in redacted


def test_redacts_generic_credential_assignment():
    redacted, count = secrets.redact('password = "supersecretvalue123"')
    assert count == 1
    assert "supersecretvalue123" not in redacted


def test_no_redaction_leaves_text_and_count_unchanged():
    text = "def add(a, b):\n    return a + b\n"
    redacted, count = secrets.redact(text)
    assert redacted == text
    assert count == 0


def test_multiple_secrets_all_counted():
    text = "AKIAABCDEFGHIJKLMNOP\nxoxb-123-abc\n"
    redacted, count = secrets.redact(text)
    assert count == 2
