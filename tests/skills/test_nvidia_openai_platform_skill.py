import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "optional-skills" / "mlops" / "nvidia-openai-platform" / "scripts" / "publish_to_openai.py"
spec = importlib.util.spec_from_file_location("publish_to_openai", SCRIPT)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def make_skill(tmp_path: Path) -> Path:
    root = tmp_path / "example-skill"
    root.mkdir()
    (root / "SKILL.md").write_text(
        "---\nname: example-skill\ndescription: A reviewed example skill.\n---\n\n# Example\n",
        encoding="utf-8",
    )
    (root / "skill-card.md").write_text("Review metadata.\n", encoding="utf-8")
    (root / "skill.oms.sig").write_text("signature placeholder\n", encoding="utf-8")
    (root / "references").mkdir()
    (root / "references" / "guide.md").write_text("Reference text.\n", encoding="utf-8")
    return root


def test_builds_one_top_level_zip_and_keeps_provenance_files(tmp_path):
    archive_bytes, meta = module.build_skill_zip(make_skill(tmp_path))
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        names = set(archive.namelist())
    assert "example-skill/SKILL.md" in names
    assert "example-skill/skill-card.md" in names
    assert "example-skill/skill.oms.sig" in names
    assert "example-skill/references/guide.md" in names
    assert meta["file_count"] == 4
    assert len(meta["sha256"]) == 64


def test_parses_block_scalar_description(tmp_path):
    root = make_skill(tmp_path)
    (root / "SKILL.md").write_text(
        "---\nname: example-skill\ndescription: >-\n  First line.\n  Second line.\n---\n# Body\n",
        encoding="utf-8",
    )
    _, metadata = module.build_skill_zip(root)
    assert metadata["description"] == "First line. Second line."


def test_rejects_missing_manifest(tmp_path):
    root = tmp_path / "missing"
    root.mkdir()
    with pytest.raises(module.PublishError, match="SKILL.md"):
        module.build_skill_zip(root)


def test_rejects_nested_second_manifest(tmp_path):
    root = make_skill(tmp_path)
    (root / "references" / "SKILL.md").write_text(
        "---\nname: nested\ndescription: Nested.\n---\n", encoding="utf-8"
    )
    with pytest.raises(module.PublishError, match="root"):
        module.build_skill_zip(root)


def test_rejects_secret_files(tmp_path):
    root = make_skill(tmp_path)
    (root / ".env.production").write_text("OPENAI_API_KEY=not-a-real-key\n", encoding="utf-8")
    with pytest.raises(module.PublishError, match="secret file"):
        module.build_skill_zip(root)


def test_dry_run_does_not_call_network_or_require_key(tmp_path, monkeypatch, capsys):
    root = make_skill(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch.object(module, "publish_skill", side_effect=AssertionError("network must not run")):
        assert module.main(["--skill-dir", str(root)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["action"] == "dry_run"
    assert output["name"] == "example-skill"


def test_publish_requires_environment_key(tmp_path, monkeypatch, capsys):
    root = make_skill(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with patch.object(module, "publish_skill", side_effect=AssertionError("must not call API")):
        assert module.main(["--skill-dir", str(root), "--publish"]) == 2
    assert "OPENAI_API_KEY" in capsys.readouterr().err


def test_publishes_new_skill_and_verifies_returned_version(tmp_path):
    archive_bytes, _ = module.build_skill_zip(make_skill(tmp_path))
    calls = []

    def fake_request(path, api_key, **kwargs):
        calls.append((path, kwargs))
        if path == "/skills":
            assert kwargs["body"].startswith(b"------OpenAISkillBundleBoundary")
            assert b'name="files"' in kwargs["body"]
            return {"id": "skill_123", "latest_version": "1"}
        if path == "/skills/skill_123/versions/1":
            return {"name": "example-skill", "description": "A reviewed example skill."}
        raise AssertionError(path)

    with patch.object(module, "_api_request", side_effect=fake_request):
        result = module.publish_skill(archive_bytes, "example-skill", "test-secret")
    assert result["skill_id"] == "skill_123"
    assert result["version"] == "1"
    assert result["verified"] is True
    assert [call[0] for call in calls] == ["/skills", "/skills/skill_123/versions/1"]


def test_adds_version_to_existing_skill_without_changing_default(tmp_path):
    archive_bytes, _ = module.build_skill_zip(make_skill(tmp_path))
    calls = []

    def fake_request(path, api_key, **kwargs):
        calls.append(path)
        if path == "/skills/skill_existing/versions":
            return {"skill_id": "skill_existing", "version": "2"}
        if path == "/skills/skill_existing/versions/2":
            return {"name": "example-skill"}
        raise AssertionError(path)

    with patch.object(module, "_api_request", side_effect=fake_request):
        result = module.publish_skill(archive_bytes, "example-skill", "test-secret", "skill_existing")
    assert result["version"] == "2"
    assert calls == ["/skills/skill_existing/versions", "/skills/skill_existing/versions/2"]
