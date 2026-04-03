from __future__ import annotations
from pathlib import Path
from dataclasses import dataclass, field
import yaml

DEFAULT_IGNORE = [
    "node_modules/**",
    "dist/**",
    "build/**",
    ".next/**",
    "__pycache__/**",
    "*.min.js",
    "*.min.css",
    ".vtrigger/**",
    ".git/**",
    "venv/**",
    ".venv/**",
    "env/**",
]


@dataclass
class Thresholds:
    max_file_lines: int = 500
    max_function_lines: int = 100
    max_class_methods: int = 20
    duplication_min_copies: int = 3


@dataclass
class Config:
    ignore: list[str] = field(default_factory=lambda: list(DEFAULT_IGNORE))
    thresholds: Thresholds = field(default_factory=Thresholds)
    disabled_detectors: list[str] = field(default_factory=list)
    allowlist: dict[str, list[str]] = field(default_factory=dict)

    @classmethod
    def load(cls, project_path: Path) -> Config:
        config_path = project_path / ".vtrigger" / "config.yaml"
        if not config_path.exists():
            return cls()

        with open(config_path) as f:
            data = yaml.safe_load(f) or {}

        config = cls()
        if "ignore" in data:
            config.ignore = DEFAULT_IGNORE + data["ignore"]
        if "thresholds" in data:
            t = data["thresholds"]
            config.thresholds = Thresholds(
                max_file_lines=t.get("max_file_lines", 500),
                max_function_lines=t.get("max_function_lines", 100),
                max_class_methods=t.get("max_class_methods", 20),
                duplication_min_copies=t.get("duplication_min_copies", 3),
            )
        if "detectors" in data and "disabled" in data["detectors"]:
            config.disabled_detectors = data["detectors"]["disabled"]
        if "allowlist" in data:
            config.allowlist = data["allowlist"]

        return config
