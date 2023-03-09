#!/usr/bin/env python3
"""Generate updated constraint and requirements files."""
from __future__ import annotations

import json
import os
import re
import sys
import tomllib
from pathlib import Path

PACKAGE_REGEX = re.compile(r"^(?:--.+\s)?([-_\.\w\d]+).*==.+$")


def gather_core_requirements() -> list[str]:
    """Gather core requirements out of pyproject.toml."""
    with open("pyproject.toml", "rb") as fp:
        data = tomllib.load(fp)
    dependencies: list[str] = data["project"]["optional-dependencies"]["server"]
    dependencies += data["project"]["dependencies"]
    dependencies += data["project"]["optional-dependencies"]["test"]
    return dependencies


def gather_requirements_from_manifests() -> list[str]:
    """Gather all of the requirements from provider manifests."""
    dependencies: list[str] = []
    providers_path = "music_assistant/server/providers"
    for dir_str in os.listdir(providers_path):
        dir_path = os.path.join(providers_path, dir_str)
        if not os.path.isdir(dir_path):
            continue
        # get files in subdirectory
        for file_str in os.listdir(dir_path):
            file_path = os.path.join(dir_path, file_str)
            if not os.path.isfile(file_path):
                continue
            if file_str != "manifest.json":
                continue

            with open(file_path) as _file:
                provider_manifest = json.loads(_file.read())
                dependencies += provider_manifest["requirements"]
    return dependencies


def main() -> int:
    """Run the script."""
    if not os.path.isfile("requirements_all.txt"):
        print("Run this from MA root dir")
        return 1

    core_reqs = gather_core_requirements()
    extra_reqs = gather_requirements_from_manifests()

    # use intermediate dict to detect duplicates
    # TODO: compare versions and only store most recent
    final_requirements: dict[str, str] = {}
    for req_str in core_reqs + extra_reqs:
        if match := PACKAGE_REGEX.search(req_str):
            package_name = match.group(1).lower().replace("_", "-")
        elif package_name in final_requirements:
            # duplicate package without version is safe to ignore
            continue
        else:
            print("Found requirement without version specifier: %s" % req_str)
            package_name = req_str

        existing = final_requirements.get(package_name)
        if existing:
            print("WARNING: ignore duplicate package: %s - existing: %s" % package_name, existing)
            continue
        final_requirements[package_name] = req_str

    content = "# WARNING: this file is autogenerated!\n\n"
    for req_key in sorted(final_requirements):
        req_str = final_requirements[req_key]
        content += f"{req_str}\n"
    Path("requirements_all.txt").write_text(content)

    return 0


if __name__ == "__main__":
    sys.exit(main())