#!/usr/bin/env python3
"""
torymemory_redact_secrets.py

Conservative secret redaction for ToryMemory ingestion.
Used by torymemory_ingest_session.py before any text is sent to :1128 or
written to the local queue. Stdlib only, Python 3.9 compatible.

Design: mask VALUES, keep structure so the curator can still see context.
False positives (over-redaction) are acceptable; leaking a credential is not.

CLI:  cat file | python3 torymemory_redact_secrets.py
API:  from torymemory_redact_secrets import redact
"""
import re
import sys

REDACTED = "[REDACTED]"

# Order matters: most specific first.
_PATTERNS = [
    # PEM / private key blocks
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL), REDACTED),
    # Authorization: Bearer <token>
    (re.compile(r"(?i)(authorization\s*:\s*bearer\s+)[A-Za-z0-9._\-]+"), r"\1" + REDACTED),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"), "bearer " + REDACTED),
    # OpenAI-style sk- keys
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{16,}\b"), REDACTED),
    # AWS access key id
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b"), REDACTED),
    # GitHub / Notion / generic prefixed tokens (ntn_ = Notion internal integration secret)
    (re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat|xox[baprs]|glpat|ntn)[-_][A-Za-z0-9._\-]{12,}\b"), REDACTED),
    # JWTs
    (re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}\b"), REDACTED),
    # Google OAuth access / refresh tokens
    (re.compile(r"\bya29\.[A-Za-z0-9._\-]{20,}"), REDACTED),
    (re.compile(r"\b1//[0-9A-Za-z._\-]{20,}"), REDACTED),
    # Google OAuth client secret + API key (Gmail/Calendar/Drive 연동)
    (re.compile(r"\bGOCSPX-[A-Za-z0-9_\-]{16,}"), REDACTED),
    (re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}\b"), REDACTED),
    # Slack app-level + legacy tokens (xoxa/xoxr/xoxs 보강)
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{8,}"), REDACTED),
    # 연결 URI 의 비밀번호 (postgres://user:pass@host, mongodb+srv://, redis:// 등)
    (re.compile(r"(?i)\b([a-z][a-z0-9+]{1,20}://[^/\s:@]+):([^@\s/]{3,})@"), r"\1:" + REDACTED + "@"),
    # URL 쿼리 파라미터로 들어간 키/토큰 (?api_key=..., &token=..., &secret=...)
    (re.compile(r"(?i)([?&](?:api[_-]?key|access[_-]?token|auth[_-]?token|token|secret|signature|sig|apikey|key)=)[^&\s\"']{8,}"), r"\1" + REDACTED),
]

# KEY=VALUE / "key": "value" where KEY looks sensitive -> mask the value only.
_SENSITIVE_KEY = re.compile(
    r"(?i)(?P<key>[A-Za-z0-9_\-]*"
    r"(?:api[_\-]?key|secret|token|password|passwd|pwd|access[_\-]?key|"
    r"private[_\-]?key|client[_\-]?secret|auth|credential|bearer)"
    r"[A-Za-z0-9_\-]*)"
    r"(?P<sep>\s*[:=]\s*)"
    r"(?P<val>\"[^\"]*\"|'[^']*'|[^\s\"'<>,}]+)"
)

# Bare long high-entropy hex/base64 blobs (e.g. proxy keys). 32+ chars.
_BARE_BLOB = re.compile(r"\b[A-Fa-f0-9]{32,}\b|\b[A-Za-z0-9+/]{40,}={0,2}\b")


def _mask_kv(m):
    return "{}{}{}".format(m.group("key"), m.group("sep"), REDACTED)


def redact(text):
    """Return text with secret-looking values masked. Never raises."""
    if not text:
        return text
    try:
        s = str(text)
        for pat, repl in _PATTERNS:
            s = pat.sub(repl, s)
        s = _SENSITIVE_KEY.sub(_mask_kv, s)
        s = _BARE_BLOB.sub(REDACTED, s)
        return s
    except Exception:
        # Fail closed: if redaction blows up, drop the body rather than risk a leak.
        return REDACTED


if __name__ == "__main__":
    data = sys.stdin.read()
    sys.stdout.write(redact(data))
