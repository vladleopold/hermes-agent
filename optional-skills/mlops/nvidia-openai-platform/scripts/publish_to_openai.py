#!/usr/bin/env python3
"""Validate and explicitly publish one Agent Skill bundle to OpenAI Platform.

Dry-run is the default. Publishing requires --publish and OPENAI_API_KEY.
Only the Python standard library is required.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

API_BASE = "https://api.openai.com/v1"
MAX_FILES = 500
MAX_UNCOMPRESSED_BYTES = 25 * 1024 * 1024
MAX_ZIP_BYTES = 50 * 1024 * 1024
EXCLUDED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"}
SECRET_NAMES = {".env", ".env.local", ".netrc", "credentials.json", "id_rsa", "id_ed25519"}
SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class PublishError(Exception):
    """A safe, user-facing validation or API error."""


def _is_secret_file(path: Path) -> bool:
    name = path.name.lower()
    return name in SECRET_NAMES or name.endswith(tuple(SECRET_SUFFIXES)) or name.startswith(".env.")


def _manifest(skill_dir: Path) -> tuple[str, str]:
    manifest = skill_dir / "SKILL.md"
    if not manifest.is_file():
        raise PublishError("Skill directory must contain SKILL.md at its root.")
    try:
        content = manifest.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise PublishError(f"Cannot read SKILL.md: {exc}") from exc
    if not content.startswith("---\n") and not content.startswith("---\r\n"):
        raise PublishError("SKILL.md must start with YAML front matter delimited by ---.")
    lines = content.splitlines()
    try:
        end = next(i for i in range(1, len(lines)) if lines[i].strip() == "---")
    except StopIteration as exc:
        raise PublishError("SKILL.md front matter has no closing --- delimiter.") from exc
    frontmatter = lines[1:end]
    name_match = next((re.match(r"^name:\s*(.*?)\s*$", line) for line in frontmatter if line.startswith("name:")), None)
    desc_match = next((re.match(r"^description:\s*(.*?)\s*$", line) for line in frontmatter if line.startswith("description:")), None)
    if not name_match or not desc_match:
        raise PublishError("SKILL.md front matter must define both name and description.")
    name = name_match.group(1).strip().strip("\"'")
    description = desc_match.group(1).strip().strip("\"'")
    if not NAME_RE.fullmatch(name) or len(name) > 64:
        raise PublishError("Skill name must be lowercase letters/numbers with hyphens, up to 64 characters.")
    if description in {">", ">-", ">+", "|", "|-", "|+"}:
        desc_index = frontmatter.index(next(line for line in frontmatter if line.startswith("description:")))
        description = " ".join(
            line.strip() for line in frontmatter[desc_index + 1:]
            if line.startswith(("  ", "\t"))
        ).strip()
    if not description:
        raise PublishError("Skill description must not be empty.")
    return name, description


def build_skill_zip(skill_dir: str | Path) -> tuple[bytes, dict[str, Any]]:
    """Validate a skill folder and return its ZIP bytes plus safe metadata."""
    requested_root = Path(skill_dir).expanduser()
    # Reject symlinked path components, not only a symlink at the leaf.
    # Otherwise a seemingly ordinary skill path could resolve through a
    # symlinked parent into an unintended directory.
    for component in (requested_root, *requested_root.parents):
        if component.is_symlink():
            raise PublishError("Skill directory must not be a symlink; symlink components are not allowed.")
    root = requested_root.resolve(strict=True)
    if not root.is_dir():
        raise PublishError("--skill-dir must point to a directory.")

    name, description = _manifest(root)
    entries: list[tuple[Path, str]] = []
    total_bytes = 0
    skill_md_count = 0

    for current in root.rglob("*"):
        relative = current.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        if current.is_symlink():
            raise PublishError(f"Symlinks are not allowed in skill bundles: {relative.as_posix()}")
        if current.is_dir():
            continue
        if not current.is_file():
            raise PublishError(f"Unsupported file type in skill bundle: {relative.as_posix()}")
        if _is_secret_file(current):
            raise PublishError(f"Potential secret file blocked from upload: {relative.as_posix()}")
        posix_path = PurePosixPath(relative.as_posix())
        if posix_path.is_absolute() or ".." in posix_path.parts:
            raise PublishError(f"Unsafe path in skill bundle: {relative.as_posix()}")
        if current.name.lower() == "skill.md":
            skill_md_count += 1
            if posix_path.as_posix().lower() != "skill.md":
                raise PublishError("The only SKILL.md must be at the skill directory root.")
        size = current.stat().st_size
        total_bytes += size
        if total_bytes > MAX_UNCOMPRESSED_BYTES:
            raise PublishError("Uncompressed skill bundle exceeds OpenAI's 25 MB limit.")
        entries.append((current, f"{root.name}/{posix_path.as_posix()}"))
        if len(entries) > MAX_FILES:
            raise PublishError("Skill bundle exceeds OpenAI's 500-file limit.")

    if skill_md_count != 1:
        raise PublishError("OpenAI skill bundle must contain exactly one SKILL.md.")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for file_path, archive_name in sorted(entries, key=lambda item: item[1]):
            # Filesystem permissions and ownership are not portable skill content.
            archive.writestr(archive_name, file_path.read_bytes())
    zip_bytes = buffer.getvalue()
    if len(zip_bytes) > MAX_ZIP_BYTES:
        raise PublishError("Compressed skill bundle exceeds OpenAI's 50 MB ZIP limit.")

    metadata = {
        "name": name,
        "description": description,
        "file_count": len(entries),
        "uncompressed_bytes": total_bytes,
        "zip_bytes": len(zip_bytes),
        "sha256": hashlib.sha256(zip_bytes).hexdigest(),
    }
    return zip_bytes, metadata


def _api_request(path: str, api_key: str, *, body: bytes | None = None,
                 content_type: str | None = None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}
    if content_type:
        headers["Content-Type"] = content_type
    request = urllib.request.Request(
        API_BASE + path, data=body, headers=headers,
        method="POST" if body is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            payload = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as exc:
        try:
            error_data = json.loads(exc.read(16 * 1024).decode("utf-8", errors="replace"))
            detail = error_data.get("error")
            message = (detail.get("message") if isinstance(detail, dict) else None) or error_data.get("message") or "request rejected"
        except Exception:
            message = "request rejected"
        raise PublishError(f"OpenAI API returned HTTP {exc.code}: {message}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise PublishError(f"OpenAI API request failed: {exc}") from exc
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublishError("OpenAI API returned an invalid JSON response.") from exc


def _multipart_zip(
    zip_bytes: bytes, bundle_name: str, *, default: bool | None = None
) -> tuple[bytes, str]:
    boundary = "----OpenAISkillBundleBoundary7MA4YWxkTrZu0gW"
    filename = f"{bundle_name}.zip"
    fields = b""
    if default is not None:
        value = "true" if default else "false"
        fields = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="default"\r\n\r\n'
            f"{value}\r\n"
        ).encode("ascii")
    file_part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="files"; filename="{filename}"\r\n'
        "Content-Type: application/zip\r\n\r\n"
    ).encode("utf-8")
    body = fields + file_part + zip_bytes + f"\r\n--{boundary}--\r\n".encode("ascii")
    return body, f"multipart/form-data; boundary={boundary}"


def publish_skill(zip_bytes: bytes, name: str, api_key: str, skill_id: str | None = None) -> dict[str, Any]:
    if skill_id:
        path = f"/skills/{urllib.parse.quote(skill_id, safe='')}/versions"
    else:
        path = "/skills"
    body, content_type = _multipart_zip(zip_bytes, name, default=False if skill_id else None)
    uploaded = _api_request(path, api_key, body=body, content_type=content_type)
    result_skill_id = uploaded.get("skill_id") or uploaded.get("id") or skill_id
    version = uploaded.get("version") or uploaded.get("latest_version") or uploaded.get("default_version")
    if not result_skill_id:
        raise PublishError("Upload response did not include a skill ID.")
    encoded_id = urllib.parse.quote(result_skill_id, safe="")
    if not version:
        current = _api_request(f"/skills/{encoded_id}", api_key)
        version = current.get("latest_version") or current.get("default_version")
    if not version:
        raise PublishError("Upload succeeded, but the API response did not include a version to verify.")

    version_path = f"/skills/{encoded_id}/versions/{urllib.parse.quote(str(version), safe='')}"
    last_error: PublishError | None = None
    for delay in (0.0, 0.5, 1.0, 2.0, 4.0):
        if delay:
            time.sleep(delay)
        try:
            verified = _api_request(version_path, api_key)
            return {
                "skill_id": result_skill_id,
                "version": str(version),
                "name": verified.get("name") or uploaded.get("name") or name,
                "description": verified.get("description") or uploaded.get("description"),
                "verified": True,
            }
        except PublishError as exc:
            if "HTTP 404" not in str(exc):
                raise
            last_error = exc
    raise PublishError(f"Upload returned a version, but it was not retrievable after retries: {last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate or publish one skill bundle to OpenAI Platform.")
    parser.add_argument("--skill-dir", required=True, help="Path to one installed skill directory containing a root SKILL.md")
    parser.add_argument("--skill-id", help="Existing OpenAI skill ID; adds an immutable version instead of creating a new skill")
    parser.add_argument("--publish", action="store_true", help="Perform the upload (otherwise this command is a local dry-run)")
    args = parser.parse_args(argv)
    try:
        zip_bytes, metadata = build_skill_zip(args.skill_dir)
        if not args.publish:
            print(json.dumps({"action": "dry_run", **metadata}, indent=2))
            return 0
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise PublishError("OPENAI_API_KEY is missing from the protected runtime environment.")
        result = publish_skill(zip_bytes, metadata["name"], api_key, args.skill_id)
        print(json.dumps({"action": "published", **metadata, **result}, indent=2))
        return 0
    except (PublishError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
