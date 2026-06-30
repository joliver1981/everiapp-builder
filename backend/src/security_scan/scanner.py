"""Rule-based static scanner over generated app source.

Design goals:
  * **Low false positives.** A noisy gate gets disabled. Every rule is tuned to
    fire on genuinely risky code, using word boundaries and suppression hints to
    avoid flagging legitimate patterns (input type="password", `Deleted N rows`,
    reading from `import.meta.env`, etc.).
  * **No external deps.** Pure-Python regex — no semgrep/node toolchain to ship
    on-prem. The rule set is intentionally small and explainable.
  * **Explainable.** Each finding names the rule, the file, the line, a snippet,
    and a one-line remediation so a developer can fix it fast.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..config import settings

# Severity ladder. The publish gate blocks at/above a configurable rung.
SEVERITY_ORDER = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

# Only these extensions are scanned (source the LLM writes). package-lock and
# binaries are skipped. We cap per-file size so a giant vendored file can't
# stall a publish.
_SCAN_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx", ".html", ".mjs", ".cjs"}
_MAX_FILE_BYTES = 512 * 1024
_SKIP_DIRS = {"node_modules", "dist", ".git", ".vite", "build", "coverage"}


@dataclass
class Rule:
    id: str
    severity: str
    title: str
    remediation: str
    # Either a compiled regex applied per-line, or a custom per-line predicate.
    pattern: re.Pattern | None = None
    multiline: bool = False  # apply `pattern` over the whole file instead of per line
    # If any of these substrings is present on the matched line, the match is
    # suppressed (used to whitelist obviously-safe forms).
    suppress_if: tuple[str, ...] = ()
    # Optional extra predicate: given the matched line, return True to KEEP.
    keep_if: Callable[[str], bool] | None = None


@dataclass
class Finding:
    rule_id: str
    severity: str
    title: str
    remediation: str
    file: str
    line: int
    snippet: str

    def to_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "title": self.title,
            "remediation": self.remediation,
            "file": self.file,
            "line": self.line,
            "snippet": self.snippet,
        }


@dataclass
class ScanReport:
    findings: list[Finding] = field(default_factory=list)
    scanned_files: int = 0

    @property
    def counts(self) -> dict[str, int]:
        out = {k: 0 for k in SEVERITY_ORDER}
        for f in self.findings:
            out[f.severity] = out.get(f.severity, 0) + 1
        return out

    @property
    def max_severity(self) -> str | None:
        if not self.findings:
            return None
        return max(self.findings, key=lambda f: SEVERITY_ORDER.get(f.severity, 0)).severity

    def at_or_above(self, threshold: str) -> list[Finding]:
        floor = SEVERITY_ORDER.get(threshold, 3)
        return [f for f in self.findings if SEVERITY_ORDER.get(f.severity, 0) >= floor]

    def to_dict(self) -> dict:
        return {
            "scanned_files": self.scanned_files,
            "finding_count": len(self.findings),
            "max_severity": self.max_severity,
            "counts": self.counts,
            "findings": [f.to_dict() for f in self.findings],
        }


def _looks_like_placeholder(value: str) -> bool:
    """Heuristic: is the quoted literal an obvious non-secret placeholder?"""
    low = value.lower()
    placeholders = (
        "your", "example", "changeme", "change-me", "placeholder", "redacted",
        "xxxx", "<", "todo", "secret-here", "dummy", "test", "...", "abc123",
        "******", "{{", "process.env", "import.meta",
    )
    return any(p in low for p in placeholders)


# ---------------------------------------------------------------------------
# Rule set
# ---------------------------------------------------------------------------
_SECRET_KEY_RE = re.compile(
    r"""(?ix)
    \b(api[_-]?key|apikey|client[_-]?secret|secret[_-]?key|secret|password|passwd|
       access[_-]?token|auth[_-]?token|private[_-]?key|bearer)\b
    \s*[:=]\s*
    (["'])(?P<val>[^"']{12,})\2
    """
)


def _secret_keep(line: str) -> bool:
    """Keep only if the assigned literal looks like a real secret."""
    m = _SECRET_KEY_RE.search(line)
    if not m:
        return False
    val = m.group("val")
    if _looks_like_placeholder(val):
        return False
    # Reading from env is fine; only literal assignment is flagged.
    if "process.env" in line or "import.meta.env" in line:
        return False
    return True


RULES: list[Rule] = [
    Rule(
        id="no-eval",
        severity="high",
        title="Use of eval()",
        remediation="Remove eval(); parse data with JSON.parse or restructure the logic.",
        pattern=re.compile(r"\beval\s*\("),
        suppress_if=("// eval", "eval is", "medieval", ".eval("),
    ),
    Rule(
        id="no-function-constructor",
        severity="high",
        title="Dynamic code via new Function()",
        remediation="Avoid the Function constructor; it executes arbitrary strings like eval.",
        pattern=re.compile(r"\bnew\s+Function\s*\("),
    ),
    Rule(
        id="aws-access-key",
        severity="critical",
        title="Hardcoded AWS access key id",
        remediation="Move the credential to a platform Secret; never embed cloud keys in app code.",
        pattern=re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    ),
    Rule(
        id="openai-key",
        severity="critical",
        title="Hardcoded OpenAI API key",
        remediation="Move the key to a platform Secret and read it server-side.",
        pattern=re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
        suppress_if=("sk-your", "sk-xxx", "sk-..."),
    ),
    Rule(
        id="google-api-key",
        severity="critical",
        title="Hardcoded Google API key",
        remediation="Move the key to a platform Secret; restrict and rotate the exposed key.",
        pattern=re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ),
    Rule(
        id="slack-token",
        severity="critical",
        title="Hardcoded Slack token",
        remediation="Revoke the token and store it as a platform Secret.",
        pattern=re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b"),
    ),
    Rule(
        id="private-key-block",
        severity="critical",
        title="Embedded private key",
        remediation="Never ship private keys in app code; store them as Secrets.",
        pattern=re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    ),
    Rule(
        id="hardcoded-secret",
        severity="high",
        title="Hardcoded secret literal",
        remediation="Read secrets from a platform Secret / env var, not a string literal.",
        pattern=_SECRET_KEY_RE,
        keep_if=_secret_keep,
    ),
    Rule(
        id="sql-string-interpolation",
        severity="high",
        title="SQL built with string interpolation",
        remediation="Use parameterized queries (:param bindings), never interpolate values into SQL.",
        pattern=re.compile(
            r"`[^`]*\b(?:SELECT|INSERT|UPDATE|DELETE|DROP|WHERE|FROM)\b[^`]*\$\{[^`]*`",
            re.IGNORECASE | re.DOTALL,
        ),
        multiline=True,
    ),
    Rule(
        id="dangerously-set-inner-html",
        severity="medium",
        title="dangerouslySetInnerHTML",
        remediation="Render text directly, or sanitize HTML (e.g. DOMPurify) before injecting it.",
        pattern=re.compile(r"dangerouslySetInnerHTML"),
    ),
    Rule(
        id="document-write",
        severity="medium",
        title="document.write()",
        remediation="Manipulate the DOM via React state instead of document.write.",
        pattern=re.compile(r"document\.write\s*\("),
    ),
    Rule(
        id="secret-in-web-storage",
        severity="medium",
        title="Secret stored in localStorage/sessionStorage",
        remediation="Web storage is readable by any script (XSS). Keep tokens in memory or httpOnly cookies.",
        pattern=re.compile(
            r"(?:localStorage|sessionStorage)\.setItem\(\s*[\"'][^\"']*"
            r"(?:token|secret|password|jwt|api[_-]?key)[^\"']*[\"']",
            re.IGNORECASE,
        ),
    ),
    Rule(
        id="insecure-http-url",
        severity="low",
        title="Insecure http:// URL",
        remediation="Use https:// for network calls; http exposes data in transit.",
        pattern=re.compile(r"[\"']http://(?!localhost|127\.0\.0\.1)[^\"']+[\"']"),
    ),
    Rule(
        id="blank-target-no-noopener",
        severity="low",
        title='target="_blank" without rel="noopener"',
        remediation='Add rel="noopener noreferrer" to prevent reverse-tabnabbing.',
        pattern=re.compile(r"target\s*=\s*[\"']_blank[\"']"),
        keep_if=lambda line: "noopener" not in line,
    ),
]


def _scan_file(rel_path: str, text: str) -> list[Finding]:
    findings: list[Finding] = []
    lines = text.splitlines()
    for rule in RULES:
        if rule.pattern is None:
            continue
        if rule.multiline:
            for m in rule.pattern.finditer(text):
                line_no = text.count("\n", 0, m.start()) + 1
                snippet = lines[line_no - 1].strip() if line_no - 1 < len(lines) else m.group(0)
                findings.append(_mk(rule, rel_path, line_no, snippet))
            continue
        for i, line in enumerate(lines, start=1):
            if not rule.pattern.search(line):
                continue
            if any(s in line for s in rule.suppress_if):
                continue
            if rule.keep_if is not None and not rule.keep_if(line):
                continue
            findings.append(_mk(rule, rel_path, i, line.strip()[:240]))
    return findings


def _mk(rule: Rule, rel_path: str, line_no: int, snippet: str) -> Finding:
    return Finding(
        rule_id=rule.id,
        severity=rule.severity,
        title=rule.title,
        remediation=rule.remediation,
        file=rel_path,
        line=line_no,
        snippet=snippet,
    )


def _app_source_root(app_id: str) -> Path:
    return Path(settings.app_data_dir) / app_id / "draft" / "frontend"


def scan_app(app_id: str) -> ScanReport:
    """Walk an app's draft source tree and run every rule over each file."""
    root = _app_source_root(app_id)
    report = ScanReport()
    if not root.exists():
        return report

    # os.walk so we can PRUNE node_modules/dist/.git instead of walking into
    # them — a draft can have hundreds of vendored files that we'd otherwise
    # stat one by one (this made publish noticeably slower on real apps).
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in sorted(filenames):
            path = Path(dirpath) / fn
            if path.suffix.lower() not in _SCAN_EXTENSIONS:
                continue
            try:
                if path.stat().st_size > _MAX_FILE_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            rel = str(path.relative_to(root)).replace("\\", "/")
            report.scanned_files += 1
            report.findings.extend(_scan_file(rel, text))

    # Stable ordering: worst first, then by file/line.
    report.findings.sort(
        key=lambda f: (-SEVERITY_ORDER.get(f.severity, 0), f.file, f.line)
    )
    return report
