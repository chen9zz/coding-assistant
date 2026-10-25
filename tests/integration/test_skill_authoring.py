"""Exercise real files and durable approvals, not an in-memory approval substitute."""

import hashlib
import os
from contextlib import suppress
from uuid import uuid4

import pytest

from repopilot.domain.models import PolicyEffect, TaskEvent, TaskSpec, ToolRequest, ToolStatus
from repopilot.infrastructure.sqlite import SQLiteEventStore
from repopilot.tools.builtin import ToolRegistry
from repopilot.tools.gateway import SafeToolGateway
from repopilot.tools.policy import PolicyEngine
from repopilot.tools.redaction import Redactor
from repopilot.tools.skills import SkillCatalog, SkillReadTool

CONTENT = "---\nname: repair\ndescription: Repair with evidence\n---\nRead tests first.\n"


def setup_authoring(tmp_path):
    from repopilot.tools.skill_write import SkillWriter

    root = tmp_path / "managed-skills"
    root.mkdir(mode=0o700)
    repo = tmp_path / "repo"
    repo.mkdir()
    catalog = SkillCatalog((), write_root=root)
    writer = SkillWriter(root, catalog)
    registry = ToolRegistry()
    registry.register(writer)
    registry.register(SkillReadTool(catalog))
    store = SQLiteEventStore(tmp_path / "events.db")
    store.create_task("task", TaskSpec(repository=repo, issue="repair", test_command=("pytest",)))
    policy = PolicyEngine(repo, skill_write_preview=writer.preview)
    gateway = SafeToolGateway(store=store, registry=registry, policy=policy, redactor=Redactor())
    return root, repo, catalog, writer, store, policy, gateway


def request(writer, operation="create", **overrides):
    args = {
        "name": "repair",
        "resource": "SKILL.md",
        "content": CONTENT,
        "expected_sha256": None,
        "root_id": writer.root_id,
    }
    args.update(overrides)
    return ToolRequest(operation_id=operation, name="skill_write", arguments=args)


def approve(store, req):
    snapshot = store.get_task("task")
    store.append(
        TaskEvent(
            event_id=str(uuid4()),
            task_id="task",
            sequence=snapshot.sequence + 1,
            event_type="task.approved",
            from_state=snapshot.state,
            to_state=snapshot.state,
            payload={"operation_id": req.operation_id, "arguments_digest": req.arguments_digest},
        ),
        expected_sequence=snapshot.sequence,
    )


def test_create_requires_approval_reloads_and_replays_without_second_write(tmp_path):
    root, repo, catalog, writer, store, policy, gateway = setup_authoring(tmp_path)
    req = request(writer)
    assert policy.decide(req).effect is PolicyEffect.ASK
    assert "+Read tests first." in policy.decide(req).reason
    assert gateway.execute("task", repo, req).status is ToolStatus.APPROVAL_REQUIRED
    assert "+Read tests first." in store.events("task")[-1].payload["approval_preview"]
    assert list(root.iterdir()) == []
    approve(store, req)
    assert gateway.execute("task", repo, req).status is ToolStatus.SUCCEEDED
    path = root / "repair/SKILL.md"
    assert path.read_text() == CONTENT
    assert catalog.read("repair") == CONTENT
    before = path.stat().st_mtime_ns
    assert gateway.execute("task", repo, req).status is ToolStatus.SUCCEEDED
    assert path.stat().st_mtime_ns == before
    assert SkillCatalog((), write_root=root).read("repair") == CONTENT


def test_update_binds_old_hash_and_requires_a_new_approval(tmp_path):
    root, repo, _, writer, store, _, gateway = setup_authoring(tmp_path)
    req = request(writer)
    gateway.execute("task", repo, req)
    approve(store, req)
    gateway.execute("task", repo, req)
    update = request(
        writer,
        "update",
        content=CONTENT + "Run tests.\n",
        expected_sha256=hashlib.sha256(CONTENT.encode()).hexdigest(),
    )
    assert gateway.execute("task", repo, update).status is ToolStatus.APPROVAL_REQUIRED
    approve(store, update)
    # A legitimate local edit after approval must never be overwritten.
    path = root / "repair/SKILL.md"
    path.write_text(CONTENT + "Newer edit.\n")
    assert gateway.execute("task", repo, update).status is ToolStatus.DENIED
    assert path.read_text().endswith("Newer edit.\n")


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "../escape"},
        {"resource": "../escape.md"},
        {"resource": "/tmp/escape.md"},
        {"resource": "references/../../escape.md"},
        {"resource": "references//a.md"},
        {"resource": "scripts/a.py"},
        {"resource": "references/a.sh"},
        {"resource": "references/.hidden.md"},
        {"content": "x" * 65537},
        {"content": CONTENT + "\x00"},
        {"content": "---\nname: another\ndescription: test\n---\nbody"},
        {"root_id": "other-root"},
        {"expected_sha256": "wrong"},
        {"unexpected": True},
    ],
)
def test_invalid_targets_are_denied_without_writing(tmp_path, overrides):
    root, repo, _, writer, _, _, gateway = setup_authoring(tmp_path)
    assert gateway.execute("task", repo, request(writer, **overrides)).status is ToolStatus.DENIED
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink-dir", "symlink-file", "hardlink", "fifo"])
def test_links_and_special_files_are_never_write_targets(tmp_path, kind):
    root, repo, _, writer, _, _, gateway = setup_authoring(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    original = outside / "SKILL.md"
    original.write_text(CONTENT)
    skill = root / "repair"
    if kind == "symlink-dir":
        skill.symlink_to(outside)
    else:
        skill.mkdir(mode=0o700)
        target = skill / "SKILL.md"
        if kind == "symlink-file":
            target.symlink_to(original)
        elif kind == "hardlink":
            os.link(original, target)
        else:
            os.mkfifo(target)
    req = request(
        writer,
        content=CONTENT + "Change",
        expected_sha256=hashlib.sha256(CONTENT.encode()).hexdigest(),
    )
    assert gateway.execute("task", repo, req).status is ToolStatus.DENIED
    assert original.read_text() == CONTENT


def test_root_replacement_and_readonly_name_collision_fail_closed(tmp_path):
    root, repo, _, writer, _, _, gateway = setup_authoring(tmp_path)
    req = request(writer)
    root.rename(tmp_path / "old-root")
    root.mkdir(mode=0o700)
    assert gateway.execute("task", repo, req).status is ToolStatus.DENIED
    readonly = tmp_path / "readonly/repair"
    readonly.mkdir(parents=True)
    (readonly / "SKILL.md").write_text(CONTENT)
    from repopilot.tools.skill_write import SkillWriter

    catalog = SkillCatalog((readonly.parent,), write_root=root)
    writer = SkillWriter(root, catalog)
    with pytest.raises(ValueError):
        writer.preview(request(writer))
    assert (readonly / "SKILL.md").read_text() == CONTENT


def test_disabled_writer_and_child_cannot_gain_write_permission(tmp_path):
    _, repo, _, writer, _, _, _ = setup_authoring(tmp_path)
    from repopilot.worker import AnalysisGateway

    req = request(writer)
    assert PolicyEngine(repo).decide(req).effect is PolicyEffect.DENY
    registry = ToolRegistry()
    registry.register(writer)
    assert (
        AnalysisGateway(registry, Redactor()).execute("child", repo, req).status
        is ToolStatus.DENIED
    )
    shell = ToolRequest(operation_id="shell", name="run_command", arguments={"argv": ["sh"]})
    assert PolicyEngine(repo).decide(shell).effect is PolicyEffect.DENY


def test_updates_references_and_listing_use_real_content_hashes(tmp_path):
    from repopilot.tools.skills import SkillListTool

    root, repo, catalog, writer, store, _, gateway = setup_authoring(tmp_path)
    requests = [
        request(writer),
        request(
            writer, "reference", resource="references/guide.md", content="First inspect tests.\n"
        ),
        request(
            writer,
            "update",
            content=CONTENT + "Then verify.\n",
            expected_sha256=hashlib.sha256(CONTENT.encode()).hexdigest(),
        ),
    ]
    for req in requests:
        assert gateway.execute("task", repo, req).status is ToolStatus.APPROVAL_REQUIRED
        approve(store, req)
        assert gateway.execute("task", repo, req).status is ToolStatus.SUCCEEDED
    assert (root / "repair/references/guide.md").read_text() == "First inspect tests.\n"
    result = SkillListTool(catalog).execute(
        repo, ToolRequest(operation_id="list", name="skill_list", arguments={})
    )
    assert result.output["skills"] == [
        {"name": "repair", "description": "Repair with evidence", "writable": True}
    ]
    result = SkillReadTool(catalog).execute(
        repo, ToolRequest(operation_id="read", name="skill_read", arguments={"name": "repair"})
    )
    assert (
        result.output["sha256"] == hashlib.sha256((CONTENT + "Then verify.\n").encode()).hexdigest()
    )


def test_altered_content_does_not_reuse_approval(tmp_path):
    root, repo, _, writer, store, _, gateway = setup_authoring(tmp_path)
    original = request(writer)
    gateway.execute("task", repo, original)
    approve(store, original)
    changed = request(writer, content=CONTENT + "Different instructions.")
    # Either a conflict or a fresh approval request is safe; it must never write.
    from repopilot.infrastructure.sqlite import IdempotencyConflict

    with suppress(IdempotencyConflict):
        assert gateway.execute("task", repo, changed).status is ToolStatus.APPROVAL_REQUIRED
    assert list(root.iterdir()) == []


def test_config_wires_writer_only_when_opted_in_and_rejects_repo_overlap(tmp_path):
    from repopilot.configuration import load_config
    from repopilot.connected import connected_session
    from repopilot.evaluation.cases import load_builtin_cases

    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    config = tmp_path / "agent.toml"
    prefix = '[provider]\nprotocol="openai"\nmodel="test"\nbase_url="http://127.0.0.1:1/v1"\n'
    config.write_text(prefix + '[skills]\nwrite_root="managed"\n')
    assert load_config(config).skills.write_root == root
    with connected_session(tmp_path / "runtime", config) as (container, runtime):
        assert "skill_write" in {tool["name"] for tool in runtime.tools}
        case = load_builtin_cases()[0]
        source = case.prepare_repository(root / "candidate")
        with pytest.raises(ValueError, match="overlap"):
            container.service.orchestrator.gateway_factory(source, case.task_spec(source))
    config.write_text(prefix)
    with connected_session(tmp_path / "readonly-runtime", config) as (_, runtime):
        assert "skill_write" not in {tool["name"] for tool in runtime.tools}


def test_real_task_approval_resume_and_reload_after_session_restart(tmp_path):
    from repopilot.agent.actions import AgentAction
    from repopilot.agent.scripted import ScriptedRuntime
    from repopilot.connected import connected_session
    from repopilot.domain.models import TaskState
    from repopilot.evaluation.cases import load_builtin_cases
    from repopilot.tools.skill_files import private_root

    root = tmp_path / "managed"
    root.mkdir(mode=0o700)
    fd, root_id = private_root(root)
    os.close(fd)
    config = tmp_path / "agent.toml"
    config.write_text(
        '[provider]\nprotocol="openai"\nmodel="offline-contract"\n'
        'base_url="http://127.0.0.1:1/v1"\n[skills]\nwrite_root="managed"\n'
    )
    case = load_builtin_cases()[0]
    source = case.prepare_repository(tmp_path / "source")
    runtime_root = tmp_path / "runtime"
    write = AgentAction.tool(
        "skill_write",
        {
            "name": "repair",
            "resource": "SKILL.md",
            "content": CONTENT,
            "expected_sha256": None,
            "root_id": root_id,
        },
        operation_id="author",
    )
    with connected_session(runtime_root, config) as (container, _):
        task = container.service.create(case.task_spec(source).model_copy(update={"max_steps": 8}))
        paused = container.service.run(task.task_id, ScriptedRuntime((write,)))
        assert paused.state is TaskState.WAITING_APPROVAL
        assert not (root / "repair").exists()
        container.service.approve(task.task_id, "author")
    # A new process/session must recover the exact approved request and expose the new skill.
    reader = ScriptedRuntime(
        (
            AgentAction.tool("skill_list", {}, operation_id="list"),
            AgentAction.tool("skill_read", {"name": "repair"}, operation_id="read"),
            case.actions[0],
            AgentAction.final("Code repaired; skill created after approval."),
        )
    )
    with connected_session(runtime_root, config) as (container, _):
        result = container.service.resume(task.task_id, reader)
        assert result.state is TaskState.SUCCEEDED, [
            (e.event_type, e.payload) for e in container.service.events(task.task_id)
        ]
        history = container.store.operation_history(task.task_id)
        read = next(outcome for req, outcome in history if req.name == "skill_read")
        assert read.output["content"] == CONTENT
        assert (root / "repair/SKILL.md").read_text() == CONTENT


@pytest.mark.parametrize("fault", ["world-readable", "root-link", "root-parent-link"])
def test_insecure_writable_roots_fail_before_registration(tmp_path, fault):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    if fault == "world-readable":
        root.chmod(0o755)
    elif fault == "root-link":
        link = tmp_path / "link"
        link.symlink_to(root)
        root = link
    else:
        (root / "child").mkdir(mode=0o700)
        link = tmp_path / "link"
        link.symlink_to(root)
        root = link / "child"
    with pytest.raises((OSError, ValueError)):
        SkillCatalog((), write_root=root)


def test_busy_writer_and_stale_approved_update_never_overwrite(tmp_path):
    import fcntl

    root, repo, _, writer, store, _, gateway = setup_authoring(tmp_path)
    req = request(writer)
    gateway.execute("task", repo, req)
    approve(store, req)
    with (root / ".write.lock").open("w") as lock:
        os.chmod(root / ".write.lock", 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert gateway.execute("task", repo, req).status is ToolStatus.FAILED
        assert not (root / "repair").exists()


def test_managed_reads_reject_non_markdown(tmp_path):
    root, repo, catalog, writer, store, _, gateway = setup_authoring(tmp_path)
    req = request(writer)
    gateway.execute("task", repo, req)
    approve(store, req)
    gateway.execute("task", repo, req)
    (root / "repair/script.py").write_text("print('no')")
    result = SkillReadTool(catalog).execute(
        repo,
        ToolRequest(
            operation_id="read-script",
            name="skill_read",
            arguments={"name": "repair", "resource": "script.py"},
        ),
    )
    assert result.status is ToolStatus.FAILED


def test_new_directory_entries_are_synced(tmp_path, monkeypatch):
    root, repo, _, writer, store, _, gateway = setup_authoring(tmp_path)
    synced = []
    original = os.fsync

    def sync(fd):
        synced.append(os.fstat(fd).st_ino)
        original(fd)

    monkeypatch.setattr(os, "fsync", sync)
    for req in (
        request(writer),
        request(writer, "reference", resource="references/guide.md", content="Guide"),
    ):
        assert gateway.execute("task", repo, req).status is ToolStatus.APPROVAL_REQUIRED
        approve(store, req)
        assert gateway.execute("task", repo, req).status is ToolStatus.SUCCEEDED
    assert root.stat().st_ino in synced
    assert synced.count((root / "repair").stat().st_ino) >= 2
    assert (root / "repair/references").stat().st_ino in synced


@pytest.mark.parametrize("fault", ["directory-sync", "catalog-refresh"])
def test_published_write_fault_is_indeterminate_and_never_retried(tmp_path, monkeypatch, fault):
    from repopilot.tools.gateway import OperationInProgress

    root, repo, catalog, writer, store, _, gateway = setup_authoring(tmp_path)
    req = request(writer)
    gateway.execute("task", repo, req)
    approve(store, req)
    target = root / "repair/SKILL.md"
    method = "fsync" if fault == "directory-sync" else "refresh"
    owner = os if fault == "directory-sync" else catalog
    original = getattr(owner, method)

    def fail_after_publish(*args):
        if target.exists():
            raise OSError("injected post-publication failure")
        return original(*args)

    with monkeypatch.context() as patch:
        patch.setattr(owner, method, fail_after_publish)
        with pytest.raises(OperationInProgress, match="indeterminate"):
            gateway.execute("task", repo, req)
    assert target.read_text() == CONTENT
    assert store.get_operation("task", req.operation_id) is None
    before = target.stat().st_mtime_ns
    # Even though policy preflight now sees an existing target, do not turn the
    # unknown outcome into a committed DENIED result or execute the write again.
    with pytest.raises(OperationInProgress, match="indeterminate"):
        gateway.execute("task", repo, req)
    assert target.stat().st_mtime_ns == before
    assert store.get_operation("task", req.operation_id) is None
