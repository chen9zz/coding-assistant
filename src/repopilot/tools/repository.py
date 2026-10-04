"""Deterministic, budgeted repository intelligence for model context."""

from __future__ import annotations

import ast
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Annotated

from pydantic import Field

from repopilot.domain.models import FrozenModel
from repopilot.infrastructure.process import ProcessRunner

_IGNORED_DIRECTORIES = frozenset(
    {
        ".cache",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
    }
)
_LANGUAGES = {
    ".c": "C",
    ".cpp": "C++",
    ".go": "Go",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".py": "Python",
    ".rs": "Rust",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
}
_STOP_WORDS = frozenset(
    {"and", "bug", "fix", "for", "from", "into", "issue", "not", "the", "this", "with"}
)
_TERM = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


class Symbol(FrozenModel):
    name: str
    kind: str
    path: str
    start_line: Annotated[int, Field(ge=1)]
    end_line: Annotated[int, Field(ge=1)]


class Snippet(FrozenModel):
    path: str
    line: Annotated[int, Field(ge=1)]
    text: str
    score: Annotated[int, Field(ge=1)]


class RepoMap(FrozenModel):
    root_name: str
    files: tuple[str, ...]
    languages: dict[str, int]
    symbols: tuple[Symbol, ...]
    imports: dict[str, tuple[str, ...]]
    test_files: tuple[str, ...]
    snippets: tuple[Snippet, ...]
    capabilities: dict[str, bool]
    truncated: bool = False


class RepositoryMapper:
    """Build a compact map from real parser/search evidence."""

    def __init__(
        self,
        *,
        enable_tree_sitter: bool = True,
        runner: ProcessRunner | None = None,
    ) -> None:
        self.runner = runner or ProcessRunner()
        self._parser = self._load_python_parser() if enable_tree_sitter else None

    def build(self, root: Path, issue: str, max_chars: int = 12_000) -> RepoMap:
        if max_chars < 512:
            raise ValueError("repository map budget must be at least 512 characters")
        resolved_root = root.resolve(strict=True)
        if not resolved_root.is_dir():
            raise ValueError("repository map root must be a directory")
        paths = self._discover_files(resolved_root)
        relative_files = tuple(path.relative_to(resolved_root).as_posix() for path in paths)
        symbols: list[Symbol] = []
        imports: dict[str, tuple[str, ...]] = {}
        for path, relative in zip(paths, relative_files, strict=True):
            if path.suffix == ".py":
                source = path.read_bytes()
                if self._parser is not None:
                    symbols.extend(self._python_symbols(relative, source))
                parsed_imports = self._python_imports(source)
                if parsed_imports:
                    imports[relative] = parsed_imports

        terms = self._issue_terms(issue)
        snippets, used_ripgrep = self._relevant_snippets(
            resolved_root,
            frozenset(relative_files),
            terms,
        )
        language_counts = Counter(_LANGUAGES.get(path.suffix.lower(), "Other") for path in paths)
        test_files = tuple(
            relative
            for relative in relative_files
            if Path(relative).name.startswith("test_") or "tests" in Path(relative).parts
        )
        repo_map = RepoMap(
            root_name=resolved_root.name,
            files=relative_files,
            languages=dict(sorted(language_counts.items())),
            symbols=tuple(
                sorted(symbols, key=lambda item: (item.path, item.start_line, item.name))
            ),
            imports=dict(sorted(imports.items())),
            test_files=test_files,
            snippets=tuple(
                sorted(snippets, key=lambda item: (-item.score, item.path, item.line))
            ),
            capabilities={
                "ripgrep": used_ripgrep,
                "tree_sitter": self._parser is not None,
            },
        )
        return self._fit_budget(repo_map, max_chars)

    def _discover_files(self, root: Path) -> list[Path]:
        git_files = self._git_files(root)
        candidates = git_files if git_files is not None else list(root.rglob("*"))
        discovered: list[Path] = []
        for path in candidates:
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(root)
            except (OSError, ValueError):
                continue
            if resolved != path.absolute() or not resolved.is_file():
                continue
            relative = resolved.relative_to(root)
            if any(part in _IGNORED_DIRECTORIES for part in relative.parts):
                continue
            if resolved.stat().st_size > 256_000 or self._is_binary(resolved):
                continue
            discovered.append(resolved)
        return sorted(discovered, key=lambda path: path.relative_to(root).as_posix())

    def _git_files(self, root: Path) -> list[Path] | None:
        result = self.runner.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            timeout_seconds=5,
            max_output_bytes=4096,
        )
        if result.exit_code != 0 or Path(result.stdout.strip()).resolve() != root:
            return None
        tracked = self.runner.run(
            ["git", "ls-files", "-z"],
            cwd=root,
            timeout_seconds=10,
            max_output_bytes=2_000_000,
        )
        if tracked.exit_code != 0 or tracked.truncated:
            return None
        return [root / relative for relative in tracked.stdout.split("\x00") if relative]

    @staticmethod
    def _is_binary(path: Path) -> bool:
        try:
            return b"\x00" in path.read_bytes()[:8192]
        except OSError:
            return True

    @staticmethod
    def _load_python_parser() -> object | None:
        try:
            import tree_sitter_python
            from tree_sitter import Language, Parser

            return Parser(Language(tree_sitter_python.language()))
        except (ImportError, TypeError, ValueError):
            return None

    def _python_symbols(self, relative: str, source: bytes) -> list[Symbol]:
        if self._parser is None:
            return []
        tree = self._parser.parse(source)  # type: ignore[attr-defined]
        found: list[Symbol] = []
        stack = [tree.root_node]
        kind_by_node = {"class_definition": "class", "function_definition": "function"}
        while stack:
            node = stack.pop()
            kind = kind_by_node.get(node.type)
            if kind:
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    found.append(
                        Symbol(
                            name=source[name_node.start_byte : name_node.end_byte].decode(
                                "utf-8", errors="replace"
                            ),
                            kind=kind,
                            path=relative,
                            start_line=node.start_point.row + 1,
                            end_line=node.end_point.row + 1,
                        )
                    )
            stack.extend(reversed(node.children))
        return found

    @staticmethod
    def _python_imports(source: bytes) -> tuple[str, ...]:
        try:
            tree = ast.parse(source.decode("utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            return ()
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                imports.update(
                    f"{module}.{alias.name}".lstrip(".") for alias in node.names
                )
        return tuple(sorted(imports))

    @staticmethod
    def _issue_terms(issue: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    match.group(0).lower()
                    for match in _TERM.finditer(issue)
                    if match.group(0).lower() not in _STOP_WORDS
                }
            )
        )

    def _relevant_snippets(
        self,
        root: Path,
        allowed_files: frozenset[str],
        terms: tuple[str, ...],
    ) -> tuple[list[Snippet], bool]:
        if not terms:
            return [], shutil.which("rg") is not None
        if shutil.which("rg") is not None:
            pattern = "|".join(re.escape(term) for term in terms)
            result = self.runner.run(
                ["rg", "--json", "--ignore-case", "--regexp", pattern, "."],
                cwd=root,
                timeout_seconds=10,
                max_output_bytes=2_000_000,
            )
            if result.exit_code in {0, 1} and not result.truncated:
                return self._parse_rg(result.stdout, allowed_files, terms), True
        return self._lexical_snippets(root, allowed_files, terms), False

    @staticmethod
    def _parse_rg(
        output: str,
        allowed_files: frozenset[str],
        terms: tuple[str, ...],
    ) -> list[Snippet]:
        snippets: list[Snippet] = []
        for raw_line in output.splitlines():
            record = json.loads(raw_line)
            if record.get("type") != "match":
                continue
            data = record["data"]
            path = str(data["path"]["text"]).removeprefix("./")
            if path not in allowed_files:
                continue
            text = str(data["lines"]["text"]).rstrip("\r\n")
            lowered = text.lower()
            score = sum(lowered.count(term) for term in terms)
            snippets.append(
                Snippet(path=path, line=int(data["line_number"]), text=text, score=max(1, score))
            )
        return snippets

    @staticmethod
    def _lexical_snippets(
        root: Path,
        allowed_files: frozenset[str],
        terms: tuple[str, ...],
    ) -> list[Snippet]:
        snippets: list[Snippet] = []
        for relative in sorted(allowed_files):
            try:
                lines = (root / relative).read_text(errors="replace").splitlines()
            except OSError:
                continue
            for line_number, text in enumerate(lines, start=1):
                lowered = text.lower()
                score = sum(lowered.count(term) for term in terms)
                if score:
                    snippets.append(
                        Snippet(path=relative, line=line_number, text=text, score=score)
                    )
        return snippets

    @staticmethod
    def _fit_budget(repo_map: RepoMap, max_chars: int) -> RepoMap:
        if len(repo_map.model_dump_json()) <= max_chars:
            return repo_map
        snippets = list(repo_map.snippets)
        symbols = list(repo_map.symbols)
        files = list(repo_map.files)
        while snippets or symbols or files:
            if snippets:
                snippets.pop()
            elif symbols:
                symbols.pop()
            else:
                files.pop()
            candidate = repo_map.model_copy(
                update={
                    "files": tuple(files),
                    "symbols": tuple(symbols),
                    "snippets": tuple(snippets),
                    "truncated": True,
                }
            )
            if len(candidate.model_dump_json()) <= max_chars:
                return candidate
        raise ValueError("repository map metadata exceeds the requested budget")
