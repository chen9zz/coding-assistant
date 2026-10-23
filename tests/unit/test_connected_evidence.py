from pathlib import Path
from types import SimpleNamespace

from repopilot.domain.models import ToolRequest, ToolStatus
from repopilot.infrastructure.mcp import MCPToolAdapter


def test_structured_mcp_evidence_is_preserved() -> None:
    class Session:
        def call_tool(self, name, *, arguments):
            return SimpleNamespace(content=[], structured_content={"answer": 42}, is_error=False)

    tool = MCPToolAdapter(Session(), server_name="test", remote_name="answer")
    result = tool.execute(None, ToolRequest(operation_id="s", name=tool.name, arguments={}))
    assert result.status is ToolStatus.SUCCEEDED
    assert result.output["structured_content"] == {"answer": 42}


def test_oversized_mcp_evidence_is_explicitly_bounded() -> None:
    class Session:
        def call_tool(self, name, *, arguments):
            return SimpleNamespace(
                content=[{"type": "text", "text": "x" * 100_000}],
                structuredContent=None,
                isError=False,
            )

    tool = MCPToolAdapter(Session(), server_name="test", remote_name="answer")
    result = tool.execute(None, ToolRequest(operation_id="large", name=tool.name, arguments={}))
    assert result.status is ToolStatus.SUCCEEDED
    assert result.truncated
    assert len(result.model_dump_json()) < 14_000
    assert result.output["preview"]


def test_large_skill_is_paged_without_losing_text_or_overflowing_context(tmp_path: Path) -> None:
    from repopilot.agent.actions import AgentContext, ToolExchange
    from repopilot.tools.skills import SkillCatalog, SkillReadTool

    skill = tmp_path / "repair"
    skill.mkdir()
    expected = "---\nname: repair\ndescription: repair code\n---\n" + "x" * 30_000
    (skill / "SKILL.md").write_text(expected)
    tool = SkillReadTool(SkillCatalog((tmp_path,)))
    offset = 0
    received = ""
    while True:
        request = ToolRequest(
            operation_id=f"page-{offset}",
            name="skill_read",
            arguments={"name": "repair", "offset": offset},
        )
        result = tool.execute(tmp_path, request)
        assert result.status is ToolStatus.SUCCEEDED
        context = AgentContext.empty().with_exchange(ToolExchange(request=request, result=result))
        assert context.compact(24_000).exchanges[0].result.output == result.output
        received += result.output["content"]
        assert len(result.output["content"]) <= 8_000
        offset = result.output["next_offset"]
        if offset is None:
            break
    assert received == expected
