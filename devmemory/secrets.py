"""Best-effort secret/credential redaction for text about to be sent to an
external LLM -- currently just the commit-diff text in extract.py's
`claude -p` call, not full file content during indexing (knowledge.py never
sends file bodies anywhere).

This is defense-in-depth, not a guarantee: regex-based detection has real
false negatives (anything not matching a known shape) and occasional false
positives (a string that merely looks like a credential). Keep the pattern
list small, explicit, and named -- in the same spirit as knowledge.py's own
documented honesty about its parsing limits.
"""

import re

_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("aws_access_key_id", re.compile(r"AKIA[0-9A-Z]{16}")),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----"
            r".*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA |)PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]+")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")),
    (
        "generic_credential_assignment",
        re.compile(
            r"(?i)\b(?:api[_-]?key|secret|password|passwd|access[_-]?token)\b"
            r"\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{8,}['\"]?"
        ),
    ),
]


def redact(text: str) -> tuple[str, int]:
    """Replace any text matching a known secret/credential shape with a
    `[REDACTED:<kind>]` marker. Returns (redacted_text, number_of_redactions).
    A count of 0 means nothing matched the known patterns -- not a guarantee
    the text is secret-free.
    """
    count = 0
    for name, pattern in _PATTERNS:
        text, n = pattern.subn(f"[REDACTED:{name}]", text)
        count += n
    return text, count
