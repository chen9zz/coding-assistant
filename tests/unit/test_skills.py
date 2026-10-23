from pathlib import Path

import pytest

from repopilot.domain.models import ToolRequest, ToolStatus


def make_skill(root: Path, name: str = "repair") -> Path:
    skill = root / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >\n  Fix regressions\n  with tests.\n"
        "allowed-tools: Bash(*)\n---\nRead the failure, write a test, repair it.\n"
    )
    return skill


def test_discovery_exposes_metadata_then_loads_instructions(tmp_path: Path) -> None:
    from repopilot.tools.skills import SkillCatalog, SkillReadTool

    make_skill(tmp_path)
    catalog = SkillCatalog((tmp_path,))
    assert catalog.metadata() == [{"name": "repair", "description": "Fix regressions with tests."}]
    result = SkillReadTool(catalog).execute(
        tmp_path,
        ToolRequest(operation_id="skill-1", name="skill_read", arguments={"name": "repair"}),
    )
    assert result.status is ToolStatus.SUCCEEDED
    assert "Read the failure" in str(result.output)
    assert "Bash(*)" in str(result.output)  # Content only, never a policy grant.


def test_skill_references_are_readable_but_escape_is_denied(tmp_path: Path) -> None:
    from repopilot.tools.skills import SkillCatalog

    skill = make_skill(tmp_path / "skills")
    (skill / "references").mkdir()
    (skill / "references" / "guide.md").write_text("regression guide")
    secret = tmp_path / "secret"
    secret.write_text("not a skill")
    (skill / "escape").symlink_to(secret)
    catalog = SkillCatalog((tmp_path / "skills",))
    assert catalog.read("repair", "references/guide.md") == "regression guide"
    for path in ("../../secret", str(secret), "escape"):
        with pytest.raises(ValueError):
            catalog.read("repair", path)
    with pytest.raises(ValueError):
        catalog.read("../secret")


def test_discovery_rejects_symlinked_skills_and_duplicate_names(tmp_path: Path) -> None:
    from repopilot.tools.skills import SkillCatalog

    make_skill(tmp_path / "a")
    make_skill(tmp_path / "b")
    with pytest.raises(ValueError, match="duplicate"):
        SkillCatalog((tmp_path / "a", tmp_path / "b"))
    (tmp_path / "c").mkdir()
    (tmp_path / "c" / "repair").symlink_to(tmp_path / "a" / "repair")
    with pytest.raises(ValueError):
        SkillCatalog((tmp_path / "c",))


def test_skills_bound_content_and_validate_frontmatter(tmp_path: Path) -> None:
    from repopilot.tools.skills import SkillCatalog

    skill = make_skill(tmp_path)
    catalog = SkillCatalog((tmp_path,))
    (skill / "large.md").write_text("x" * 65_537)
    with pytest.raises(ValueError, match="large"):
        catalog.read("repair", "large.md")
    (skill / "SKILL.md").write_text("---\nname: different\ndescription: test\n---\nbody")
    with pytest.raises(ValueError):
        SkillCatalog((tmp_path,))


def test_skill_read_requires_a_known_name_and_is_read_only(tmp_path: Path) -> None:
    from repopilot.tools.policy import PolicyEngine
    from repopilot.tools.skills import SkillCatalog, SkillReadTool

    tool = SkillReadTool(SkillCatalog(()))
    request = ToolRequest(operation_id="unknown", name="skill_read", arguments={"name": "nope"})
    assert tool.execute(tmp_path, request).status is ToolStatus.FAILED
    assert PolicyEngine(tmp_path).decide(request).effect.value == "ALLOW"
    assert (
        PolicyEngine(tmp_path)
        .decide(
            ToolRequest(
                operation_id="cmd", name="run_command", arguments={"argv": ["python", "x.py"]}
            )
        )
        .effect.value
        == "ASK"
    )
