"""Redaction policy: the terms to mask and the ones to leave alone.

Persisted as JSON so decisions can be reused across files, replayed in
non-interactive runs (CI, batch processing) and audited.

Decisions are indexed by folded form (no accents, case-insensitive, collapsed
whitespace): lookup is O(1) and "Jean  DUPONT" finds the decision recorded for
"Jean Dupont".

WARNING: the policy file holds personal data in clear text. It is created with
mode 0600 and must be protected like the source document.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .matching import fold, normalize

SCHEMA_VERSION = 1


class PolicyError(RuntimeError):
    """Unreadable or inconsistent policy, with an actionable message."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


@dataclass
class Policy:
    # folded key -> original form (dict preserves insertion order)
    _redact: Dict[str, str] = field(default_factory=dict)
    _ignore: Dict[str, str] = field(default_factory=dict)
    kind_rules: Dict[str, str] = field(default_factory=dict)  # kind -> always|never
    path: Optional[Path] = None

    # ----------------------------------------------------------------- access
    @property
    def redact(self) -> List[str]:
        return list(self._redact.values())

    @property
    def ignore(self) -> List[str]:
        return list(self._ignore.values())

    def __len__(self) -> int:
        return len(self._redact)

    # ----------------------------------------------------------------- read
    @classmethod
    def load(cls, path: Optional[Path]) -> "Policy":
        if path is None:
            return cls()
        path = Path(path)
        if not path.exists():
            return cls(path=path)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PolicyError(
                f"cannot read policy: {path} ({exc})",
                hint="check the path and read permissions",
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PolicyError(
                f"policy {path}: invalid JSON at line {exc.lineno}, "
                f"column {exc.colno} ({exc.msg})",
                hint="fix the file, or delete it to start from scratch",
            ) from exc
        if not isinstance(data, dict):
            raise PolicyError(
                f"policy {path}: a JSON object is expected",
                hint='format: {"version": 1, "redact": [...], "ignore": [...]}',
            )
        version = data.get("version", SCHEMA_VERSION)
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise PolicyError(
                f"policy {path} is version {version}, tool supports "
                f"{SCHEMA_VERSION}",
                hint="upgrade the tool, or start from an empty policy",
            )

        policy = cls(path=path)
        for key in ("redact", "ignore"):
            values = data.get(key, [])
            if not isinstance(values, list):
                raise PolicyError(f"policy {path}: '{key}' must be a list")
            for value in values:
                if not isinstance(value, str):
                    continue
                (policy.add_redact if key == "redact" else policy.add_ignore)(value)
        rules = data.get("kind_rules", {})
        if isinstance(rules, dict):
            policy.kind_rules = {
                str(k): str(v) for k, v in rules.items() if v in ("always", "never")
            }
        return policy

    # ---------------------------------------------------------------- write
    def save(self, path: Optional[Path] = None) -> Optional[Path]:
        target = Path(path or self.path) if (path or self.path) else None
        if target is None:
            return None
        payload = {
            "version": SCHEMA_VERSION,
            "redact": sorted(self.redact, key=str.casefold),
            "ignore": sorted(self.ignore, key=str.casefold),
            "kind_rules": self.kind_rules,
        }
        tmp = target.with_name(target.name + ".tmp")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass  # file system without POSIX permissions
            tmp.replace(target)  # atomic replacement
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise PolicyError(
                f"cannot save policy: {target} ({exc})",
                hint="check write permissions and free disk space",
            ) from exc
        self.path = target
        return target

    # ------------------------------------------------------------ decisions
    def decision(self, value: str, kind: Optional[str] = None) -> Optional[bool]:
        """True = redact, False = leave alone, None = ask the user."""
        key = fold(value)
        if key in self._redact:
            return True
        if key in self._ignore:
            return False
        if kind:
            rule = self.kind_rules.get(kind)
            if rule == "always":
                return True
            if rule == "never":
                return False
        return None

    def add_redact(self, value: str) -> None:
        value = normalize(value)
        if not value:
            return
        key = fold(value)
        self._ignore.pop(key, None)
        self._redact.setdefault(key, value)

    def add_ignore(self, value: str) -> None:
        value = normalize(value)
        if not value:
            return
        key = fold(value)
        self._redact.pop(key, None)
        self._ignore.setdefault(key, value)

    def set_kind(self, kind: str, mode: str) -> None:
        if mode not in ("always", "never"):
            raise ValueError(f"unknown rule mode: {mode}")
        self.kind_rules[kind] = mode


def read_terms_file(path: Path) -> List[str]:
    """One term per line; a leading '#' marks a comment."""
    path = Path(path)
    if not path.exists():
        raise PolicyError(
            f"terms file not found: {path}",
            hint="pass a text file with one term per line",
        )
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(
            f"{path}: the file must be UTF-8 encoded ({exc.reason})"
        ) from exc
    except OSError as exc:
        raise PolicyError(f"{path}: cannot read file ({exc})") from exc

    terms: List[str] = []
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        value = normalize(line)
        if value:
            terms.append(value)
    if not terms:
        raise PolicyError(
            f"{path}: no usable term found",
            hint="one term per line, '#' to comment out",
        )
    return terms
