import subprocess
from pathlib import Path

import pytest

from repopilot.tools.repository import RepositoryMapper


@pytest.fixture
def sample_repo() -> Path:
    return Path(__file__).parents[1] / "fixtures" / "sample_repo"


def test_repo_map_contains_symbols_imports_tests_and_capabilities(sample_repo: Path) -> None:
    """Losing semantic indexing would force the model to search the repository blindly."""
    repo_map = RepositoryMapper().build(sample_repo, "fix divide by zero")

    assert any(
        symbol.name == "Calculator" and symbol.kind == "class" for symbol in repo_map.symbols
    )
    assert any(symbol.name == "divide" and symbol.kind == "function" for symbol in repo_map.symbols)
    assert repo_map.imports["app.py"] == ("decimal.Decimal",)
    assert repo_map.test_files == ("test_app.py",)
    assert repo_map.capabilities["tree_sitter"] is True
    assert repo_map.capabilities["ripgrep"] is True


def test_issue_terms_select_relevant_line_with_literal_evidence(sample_repo: Path) -> None:
    """Ignoring issue relevance would spend the model budget on unrelated files."""
    repo_map = RepositoryMapper().build(sample_repo, "fix divide by zero")

    matching = [snippet for snippet in repo_map.snippets if snippet.path == "app.py"]
    assert matching
    assert any("def divide" in snippet.text for snippet in matching)


def test_repo_map_is_deterministic_and_within_serialized_budget(sample_repo: Path) -> None:
    """Unstable or unbounded maps would break caching and context limits."""
    first = RepositoryMapper().build(sample_repo, "fix divide by zero", max_chars=2_000)
    second = RepositoryMapper().build(sample_repo, "fix divide by zero", max_chars=2_000)

    assert first.model_dump_json() == second.model_dump_json()
    assert len(first.model_dump_json()) <= 2_000


def test_mapper_records_lexical_fallback_when_parser_is_disabled(sample_repo: Path) -> None:
    """Pretending parsing succeeded would give the model fabricated symbol evidence."""
    repo_map = RepositoryMapper(enable_tree_sitter=False).build(
        sample_repo, "fix divide by zero"
    )

    assert repo_map.capabilities["tree_sitter"] is False
    assert repo_map.symbols == ()
    assert repo_map.files == ("app.py", "test_app.py")


def test_mapper_skips_binary_and_hidden_cache_files(tmp_path: Path) -> None:
    """Indexing generated/binary data would waste context and may expose unrelated content."""
    (tmp_path / "module.py").write_text("def useful():\n    return 1\n")
    (tmp_path / "blob.bin").write_bytes(b"\x00\x01\x02")
    cache = tmp_path / ".cache"
    cache.mkdir()
    (cache / "secret.py").write_text("SECRET = 'ignored'\n")

    repo_map = RepositoryMapper().build(tmp_path, "useful")

    assert repo_map.files == ("module.py",)


@pytest.mark.parametrize("git_backed", [False, True])
def test_mapper_never_reads_a_symlink_outside_the_repository(
    tmp_path: Path,
    git_backed: bool,
) -> None:
    """A repository link must not turn semantic indexing into a host-file read primitive."""
    outside = tmp_path / "host_secret.py"
    outside.write_text("def host_secret() -> str:\n    return 'outside'\n")
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "inside.py").write_text("def inside() -> bool:\n    return True\n")
    (repository / "linked.py").symlink_to(outside)
    if git_backed:
        subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True)
        subprocess.run(["git", "add", "."], cwd=repository, check=True)

    repo_map = RepositoryMapper().build(repository, "host secret")

    assert "linked.py" not in repo_map.files
    assert all(symbol.name != "host_secret" for symbol in repo_map.symbols)
