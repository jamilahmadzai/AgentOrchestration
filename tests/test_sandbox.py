import os
import stat

import pytest

from src.agent.sandbox import AgentSandbox, PRIVATE_DIR_MODE


pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission mode assertions require POSIX chmod semantics",
)


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_sandbox_directories_use_private_mode_under_permissive_umask(tmp_path):
    previous_umask = os.umask(0)
    try:
        sandbox = AgentSandbox(str(tmp_path / "root"))
        agent_path = sandbox.create("agent-1")
    finally:
        os.umask(previous_umask)

    assert mode(sandbox.base_path) == PRIVATE_DIR_MODE
    assert mode(agent_path) == PRIVATE_DIR_MODE


def test_existing_sandbox_root_is_tightened(tmp_path):
    root = tmp_path / "root"
    root.mkdir(mode=0o777)
    os.chmod(root, 0o777)

    sandbox = AgentSandbox(str(root))

    assert sandbox.base_path == root
    assert mode(root) == PRIVATE_DIR_MODE


def test_nested_sandbox_directories_are_tightened(tmp_path):
    previous_umask = os.umask(0)
    try:
        sandbox = AgentSandbox(str(tmp_path / "root"))
        nested_path = sandbox.create("team-a/agent-1")
    finally:
        os.umask(previous_umask)

    assert mode(sandbox.base_path) == PRIVATE_DIR_MODE
    assert mode(sandbox.base_path / "team-a") == PRIVATE_DIR_MODE
    assert mode(nested_path) == PRIVATE_DIR_MODE


def test_new_base_path_parents_are_tightened(tmp_path):
    previous_umask = os.umask(0)
    try:
        sandbox = AgentSandbox(str(tmp_path / "sandboxes" / "root"))
    finally:
        os.umask(previous_umask)

    assert mode(sandbox.base_path.parent) == PRIVATE_DIR_MODE
    assert mode(sandbox.base_path) == PRIVATE_DIR_MODE


def test_symlinked_sandbox_root_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked sandbox path"):
        AgentSandbox(str(link))


def test_non_directory_sandbox_root_is_rejected(tmp_path):
    path = tmp_path / "sandbox-file"
    path.write_text("not a directory")

    with pytest.raises(ValueError, match="not a directory"):
        AgentSandbox(str(path))


def test_symlinked_sandbox_parent_is_rejected(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)

    with pytest.raises(ValueError, match="under symlink"):
        AgentSandbox(str(link / "root"))
