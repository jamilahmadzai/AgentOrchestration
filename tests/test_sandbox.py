import os
import stat

import pytest

from src.agent.sandbox import AgentSandbox


def mode_for(path):
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes only")
def test_sandbox_directories_ignore_permissive_umask(tmp_path):
    old_umask = os.umask(0)
    try:
        sandbox = AgentSandbox(str(tmp_path / "sandbox-root"))
        agent_path = sandbox.create("agent-1")
    finally:
        os.umask(old_umask)

    assert mode_for(sandbox.base_path) == 0o700
    assert mode_for(agent_path) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes only")
def test_existing_sandbox_directories_are_tightened(tmp_path):
    base_path = tmp_path / "sandbox-root"
    agent_path = base_path / "agent-1"
    agent_path.mkdir(parents=True, mode=0o777)
    base_path.chmod(0o777)
    agent_path.chmod(0o777)

    sandbox = AgentSandbox(str(base_path))
    created = sandbox.create("agent-1")

    assert created == agent_path
    assert mode_for(base_path) == 0o700
    assert mode_for(agent_path) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission modes only")
def test_nested_sandbox_parents_are_owner_only(tmp_path):
    sandbox = AgentSandbox(str(tmp_path / "sandbox-root"))
    agent_path = sandbox.create("team-a/agent-1")

    assert mode_for(agent_path.parent) == 0o700
    assert mode_for(agent_path) == 0o700


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink checks only")
def test_existing_sandbox_symlink_is_rejected_without_chmod(tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o777)
    target.chmod(0o777)
    link = tmp_path / "sandbox-root"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        AgentSandbox(str(link))

    assert mode_for(target) == 0o777


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlink checks only")
def test_sandbox_parent_symlink_is_rejected_before_child_creation(tmp_path):
    sandbox = AgentSandbox(str(tmp_path / "sandbox-root"))
    target = tmp_path / "target"
    target.mkdir(mode=0o777)
    target.chmod(0o777)
    link = sandbox.base_path / "team-a"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlink"):
        sandbox.create("team-a/agent-1")

    assert not (target / "agent-1").exists()
    assert mode_for(target) == 0o777


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode checks only")
def test_existing_sandbox_file_is_rejected(tmp_path):
    sandbox_file = tmp_path / "sandbox-root"
    sandbox_file.write_text("not a directory", encoding="utf-8")

    with pytest.raises(ValueError, match="directory"):
        AgentSandbox(str(sandbox_file))


def test_sandbox_path_must_remain_under_base(tmp_path):
    sandbox = AgentSandbox(str(tmp_path / "sandbox-root"))

    with pytest.raises(ValueError, match="base directory"):
        sandbox.create("../outside")

    assert not (tmp_path / "outside").exists()
