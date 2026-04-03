from __future__ import annotations

import re
from pathlib import Path

from .base import Detector
from ..finding import Finding, ParsedFile

# (pattern, description, confidence)
SECRET_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"AKIA[0-9A-Z]{16}"),
        "Possible AWS access key found",
        "high",
    ),
    (
        re.compile(r"aws_secret_access_key\s*=\s*['\"][A-Za-z0-9/+=]{40}['\"]"),
        "Possible AWS secret key found",
        "high",
    ),
    (
        re.compile(r"(api[_-]?key|apikey)\s*[:=]\s*['\"][A-Za-z0-9]{20,}['\"]", re.IGNORECASE),
        "Possible API key found",
        "medium",
    ),
    (
        re.compile(
            r"(secret|password|passwd|token|auth_token|access_token)\s*[:=]\s*['\"][^'\"]{8,}['\"]",
            re.IGNORECASE,
        ),
        "Possible hardcoded secret found",
        "medium",
    ),
    (
        re.compile(r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
        "Private key found",
        "high",
    ),
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
        "Possible JWT token found",
        "medium",
    ),
    (
        re.compile(r"gh[ps]_[A-Za-z0-9]{36,}"),
        "Possible GitHub token found",
        "high",
    ),
    (
        re.compile(r"xox[bpras]-[A-Za-z0-9-]{10,}"),
        "Possible Slack token found",
        "high",
    ),
]

PLACEHOLDER_VALUES = re.compile(
    r"^(your-api-key|xxx+|TODO|CHANGEME|placeholder|test|example||sk-xxx.*)$",
    re.IGNORECASE,
)

SKIP_FILE_PATTERNS = [
    re.compile(r"(^|/)test[s_]?/"),
    re.compile(r"(^|/)__tests__/"),
    re.compile(r"/test_[^/]*\.py$"),
    re.compile(r"\.(test|spec)\.(ts|js|tsx|jsx|py)$"),
    re.compile(r"(^|/)\.env(\.local|\.example|\.sample)?$"),
]

COMMENT_PREFIX = re.compile(r"^\s*(#|//|\*)")


def _is_skipped_file(filepath: str) -> bool:
    for pattern in SKIP_FILE_PATTERNS:
        if pattern.search(filepath):
            return True
    return False


def _is_comment(line: str) -> bool:
    return bool(COMMENT_PREFIX.match(line))


def _contains_placeholder(line: str) -> bool:
    # Extract quoted values from the line and check against placeholders
    for match in re.finditer(r"['\"]([^'\"]*)['\"]", line):
        value = match.group(1)
        if PLACEHOLDER_VALUES.match(value):
            return True
    return False


def _redact_line(line: str) -> str:
    """Redact secret values in a line, showing first 4 chars + ***."""
    def _redact_quoted(m: re.Match[str]) -> str:
        quote = m.group(0)[0]
        value = m.group(1)
        if len(value) <= 4:
            return f"{quote}***{quote}"
        return f"{quote}{value[:4]}***{quote}"

    return re.sub(r"['\"]([^'\"]+)['\"]", _redact_quoted, line)


class SecretsDetector(Detector):
    name = "secrets"

    def run(self, files: list[ParsedFile]) -> list[Finding]:
        findings: list[Finding] = []

        for pf in files:
            filepath = str(pf.path)

            if _is_skipped_file(filepath):
                continue

            try:
                text = pf.path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue

            lines = text.splitlines()

            for line_num, line in enumerate(lines, start=1):
                if _is_comment(line):
                    continue

                if _contains_placeholder(line):
                    continue

                for pattern, description, confidence in SECRET_PATTERNS:
                    if pattern.search(line):
                        findings.append(
                            Finding(
                                detector="secrets",
                                category="hardcoded_secret",
                                message=description,
                                file=filepath,
                                line=line_num,
                                snippet=_redact_line(line.strip()),
                                confidence=confidence,
                            )
                        )
                        break  # one finding per line

        return findings
