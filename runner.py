#!/usr/bin/env python3
"""Recognize configured radiomics inputs in one patient directory.

This script does not compute radiomics features yet. It only inspects a patient
directory, applies the recognition rules in radiomics_config.yaml, and writes a
JSON manifest that later extraction code can consume.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:  # pragma: no cover - exercised only on machines without PyYAML
    yaml = None


DEFAULT_CONFIG = Path(__file__).with_name("radiomics_config.yaml")


@dataclass(frozen=True)
class Candidate:
    path: Path
    relative_path: str
    filename: str
    directory: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recognize ROI and image modalities in one patient directory."
    )
    parser.add_argument("patient_dir", type=Path, help="Directory for one patient.")
    parser.add_argument("output_json", type=Path, help="Where to write the manifest JSON.")
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help=f"YAML recognition config. Default: {DEFAULT_CONFIG}",
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="Write compact JSON instead of indented JSON.",
    )
    return parser.parse_args()


def load_config(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required to read the YAML config. Install it with: "
            "python3 -m pip install pyyaml"
        )
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    if not isinstance(config, dict):
        raise ValueError(f"Config must contain a YAML mapping: {path}")
    return config


def normalize_exts(config: dict[str, Any]) -> tuple[str, ...]:
    values = config.get("search", {}).get("allowed_extensions", [])
    if not isinstance(values, list) or not values:
        raise ValueError("Config key search.allowed_extensions must be a non-empty list.")
    return tuple(str(value).casefold() for value in values)


def has_allowed_extension(path: Path, allowed_extensions: tuple[str, ...]) -> bool:
    name = path.name.casefold()
    return any(name.endswith(ext) for ext in allowed_extensions)


def list_files(patient_dir: Path, allowed_extensions: tuple[str, ...]) -> list[Candidate]:
    candidates: list[Candidate] = []
    for path in sorted(patient_dir.rglob("*"), key=lambda item: str(item).casefold()):
        if not path.is_file() or not has_allowed_extension(path, allowed_extensions):
            continue
        relative = path.relative_to(patient_dir)
        parent = relative.parent.as_posix()
        candidates.append(
            Candidate(
                path=path,
                relative_path=relative.as_posix(),
                filename=path.name,
                directory="." if parent == "." else parent,
            )
        )
    return candidates


def matches_any(value: str, patterns: list[str]) -> bool:
    folded = value.casefold()
    return any(fnmatch.fnmatchcase(folded, pattern.casefold()) for pattern in patterns)


def candidate_matches(candidate: Candidate, rule: dict[str, Any]) -> bool:
    patterns = [str(item) for item in rule.get("patterns", [])]
    exclude_patterns = [str(item) for item in rule.get("exclude_patterns", [])]
    if not patterns:
        raise ValueError(f"Rule for {rule.get('label', 'unnamed entry')} has no patterns.")

    searchable_values = [candidate.filename, candidate.relative_path]
    include = any(matches_any(value, patterns) for value in searchable_values)
    exclude = any(matches_any(value, exclude_patterns) for value in searchable_values)
    return include and not exclude


def natural_path_key(value: str) -> tuple[Any, ...]:
    parts = re.split(r"(\d+)", value.casefold())
    return tuple(int(part) if part.isdigit() else part for part in parts)


def preference_key(
    candidate: Candidate,
    rule: dict[str, Any],
    preferred_directories: list[str],
) -> tuple[int, int, tuple[Any, ...]]:
    directory = candidate.directory.casefold()
    preferred_lookup = {
        str(value).strip("/").casefold() or ".": index
        for index, value in enumerate(preferred_directories)
    }
    directory_rank = preferred_lookup.get(directory, len(preferred_lookup))

    patterns = [str(item) for item in rule.get("patterns", [])]
    pattern_rank = len(patterns)
    for index, pattern in enumerate(patterns):
        if matches_any(candidate.filename, [pattern]) or matches_any(
            candidate.relative_path, [pattern]
        ):
            pattern_rank = index
            break

    return (directory_rank, pattern_rank, natural_path_key(candidate.relative_path))


def serialize_candidate(candidate: Candidate) -> dict[str, str]:
    return {
        "path": str(candidate.path),
        "relative_path": candidate.relative_path,
        "filename": candidate.filename,
        "directory": candidate.directory,
    }


def recognize_entry(
    key: str,
    rule: dict[str, Any],
    files: list[Candidate],
    preferred_directories: list[str],
) -> tuple[dict[str, Any], list[str]]:
    matching = [candidate for candidate in files if candidate_matches(candidate, rule)]
    matching.sort(key=lambda item: preference_key(item, rule, preferred_directories))

    required = bool(rule.get("required", False))
    multiple = bool(rule.get("multiple", False))
    warnings: list[str] = []

    if multiple:
        selected: list[str] | str | None = [candidate.relative_path for candidate in matching]
    else:
        selected = matching[0].relative_path if matching else None
        if len(matching) > 1:
            warnings.append(
                f"{key}: multiple candidates matched; selected {selected} by config priority."
            )

    result = {
        "label": rule.get("label", rule.get("name", key)),
        "required": required,
        "multiple": multiple,
        "missing": len(matching) == 0,
        "selected": selected,
        "candidates": [serialize_candidate(candidate) for candidate in matching],
    }
    return result, warnings


def build_manifest(patient_dir: Path, config_path: Path, config: dict[str, Any]) -> dict[str, Any]:
    allowed_extensions = normalize_exts(config)
    files = list_files(patient_dir, allowed_extensions)
    preferred_directories = [
        str(value) for value in config.get("search", {}).get("preferred_directories", ["."])
    ]

    warnings: list[str] = []
    missing_required: list[str] = []
    missing_optional: list[str] = []

    roi_rule = config.get("roi")
    if not isinstance(roi_rule, dict):
        raise ValueError("Config key roi must be a mapping.")
    roi, roi_warnings = recognize_entry("roi", roi_rule, files, preferred_directories)
    warnings.extend(roi_warnings)
    if roi["missing"]:
        (missing_required if roi["required"] else missing_optional).append("roi")

    modalities_config = config.get("modalities")
    if not isinstance(modalities_config, dict) or not modalities_config:
        raise ValueError("Config key modalities must be a non-empty mapping.")

    images: dict[str, Any] = {}
    for name, rule in modalities_config.items():
        if not isinstance(rule, dict):
            raise ValueError(f"Config modality {name} must be a mapping.")
        images[name], entry_warnings = recognize_entry(
            str(name), rule, files, preferred_directories
        )
        warnings.extend(entry_warnings)
        if images[name]["missing"]:
            target = missing_required if images[name]["required"] else missing_optional
            target.append(str(name))

    status = "ready" if not missing_required else "missing_required"
    return {
        "schema_version": config.get("schema_version", 1),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "patient_id": patient_dir.name,
        "patient_dir": str(patient_dir),
        "config_path": str(config_path),
        "status": status,
        "files_scanned": [candidate.relative_path for candidate in files],
        "roi": roi,
        "images": images,
        "missing_required": missing_required,
        "missing_optional": missing_optional,
        "warnings": warnings,
    }


def main() -> int:
    args = parse_args()
    patient_dir = args.patient_dir.resolve()
    output_json = args.output_json.resolve()
    config_path = args.config.resolve()

    if not patient_dir.is_dir():
        print(f"Patient directory not found: {patient_dir}", file=sys.stderr)
        return 2

    try:
        config = load_config(config_path)
        manifest = build_manifest(patient_dir, config_path, config)
    except Exception as exc:
        print(f"runner.py: {exc}", file=sys.stderr)
        return 1

    output_json.parent.mkdir(parents=True, exist_ok=True)
    with output_json.open("w", encoding="utf-8") as handle:
        if args.compact:
            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
        else:
            json.dump(manifest, handle, indent=2, sort_keys=True)
            handle.write("\n")

    print(f"{manifest['patient_id']}: {manifest['status']} -> {output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
