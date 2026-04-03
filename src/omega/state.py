from __future__ import annotations
from pathlib import Path
from typing import Optional
import json
from .finding import Finding


class State:
    def __init__(self, project_path: Path):
        self.dir = project_path / ".vtrigger"
        self.dir.mkdir(exist_ok=True)
        self._findings: list[Finding] = []
        self._resolved: set[str] = set()
        self._skipped: set[str] = set()
        self._current_index: int = 0
        self._load()

    def _load(self):
        findings_path = self.dir / "findings.json"
        if findings_path.exists():
            data = json.loads(findings_path.read_text())
            self._findings = [Finding.from_dict(f) for f in data]

        resolved_path = self.dir / "resolved.json"
        if resolved_path.exists():
            self._resolved = set(json.loads(resolved_path.read_text()))

        skipped_path = self.dir / "skipped.json"
        if skipped_path.exists():
            self._skipped = set(json.loads(skipped_path.read_text()))

        index_path = self.dir / "index"
        if index_path.exists():
            self._current_index = int(index_path.read_text().strip())

    def save_findings(self, findings: list[Finding]):
        self._findings = findings
        self._current_index = 0
        data = [f.to_dict() for f in findings]
        (self.dir / "findings.json").write_text(json.dumps(data, indent=2))
        (self.dir / "index").write_text("0")

    def _save_resolved(self):
        (self.dir / "resolved.json").write_text(json.dumps(list(self._resolved)))

    def _save_skipped(self):
        (self.dir / "skipped.json").write_text(json.dumps(list(self._skipped)))

    def _save_index(self):
        (self.dir / "index").write_text(str(self._current_index))

    @property
    def pending(self) -> list[Finding]:
        return [
            f for f in self._findings
            if f.hash not in self._resolved and f.hash not in self._skipped
        ]

    def next_finding(self, detector: Optional[str] = None) -> Optional[Finding]:
        pending = self.pending
        if detector:
            pending = [f for f in pending if f.detector == detector]
        return pending[0] if pending else None

    def resolve(self, finding_hash: str):
        self._resolved.add(finding_hash)
        self._save_resolved()

    def skip(self, finding_hash: str):
        self._skipped.add(finding_hash)
        self._save_skipped()

    def load_parse_cache(self) -> dict:
        """Load cached parse results. Returns {filepath: {mtime: float, data: dict}}"""
        cache_path = self.dir / "parse_cache.json"
        if cache_path.exists():
            return json.loads(cache_path.read_text())
        return {}

    def save_parse_cache(self, cache: dict):
        """Save parse cache to disk."""
        (self.dir / "parse_cache.json").write_text(json.dumps(cache))

    @property
    def stats(self) -> dict:
        total = len(self._findings)
        resolved = len([f for f in self._findings if f.hash in self._resolved])
        skipped = len([f for f in self._findings if f.hash in self._skipped])
        pending = total - resolved - skipped

        by_detector: dict[str, int] = {}
        for f in self.pending:
            by_detector[f.detector] = by_detector.get(f.detector, 0) + 1

        return {
            "total": total,
            "resolved": resolved,
            "skipped": skipped,
            "pending": pending,
            "by_detector": by_detector,
        }
