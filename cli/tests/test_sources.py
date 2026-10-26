import pytest

from llmd.cli import main
from llmd.errors import NotFoundError, UsageError
from llmd.sources import SourceRegistry, resolve_source, split_ref

from .conftest import git


@pytest.fixture
def remote(tmp_path):
    """A bare repository with a branch and a pull-request ref, like a GitHub remote."""
    work, bare = tmp_path / "work", tmp_path / "remote.git"
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    (work / "stack.yaml").write_text("kind: ConfigMap\n")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "main")
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(work, "push", "-q", str(bare), "main")
    (work / "stack.yaml").write_text("kind: Secret\n")
    git(work, "commit", "-q", "-am", "pr")
    git(work, "push", "-q", str(bare), "HEAD:refs/pull/7/head")
    return f"file://{bare}"


def test_split_ref():
    assert split_ref("https://example.invalid/r.git#refs/pull/7/head") == (
        "https://example.invalid/r.git",
        "refs/pull/7/head",
    )
    assert split_ref("/a/path") == ("/a/path", "")


def test_a_branch_and_a_pull_request_ref_resolve(remote):
    main_source = resolve_source(f"{remote}#main")
    pull = resolve_source(f"{remote}#refs/pull/7/head")
    assert (main_source.root / "stack.yaml").read_text() == "kind: ConfigMap\n"
    assert (pull.root / "stack.yaml").read_text() == "kind: Secret\n"
    assert main_source.revision != pull.revision
    assert main_source.kind == pull.kind == "git"
    assert not pull.dirty


def test_a_cached_checkout_is_reused_until_refreshed(remote):
    first = resolve_source(f"{remote}#main")
    (first.root / "stack.yaml").write_text("changed\n")
    assert resolve_source(f"{remote}#main").dirty
    assert not resolve_source(f"{remote}#main", refresh=True).dirty


def test_a_local_checkout_is_used_as_it_is(tmp_path):
    git(tmp_path, "init", "-q")
    source = resolve_source(str(tmp_path))
    assert source.kind == "local" and source.root == tmp_path.resolve()
    with pytest.raises(UsageError, match="used as it is"):
        resolve_source(f"{tmp_path}#main")
    with pytest.raises(NotFoundError):
        resolve_source(str(tmp_path / "missing"))


def test_registry_add_list_remove(tmp_path, remote):
    registry = SourceRegistry(tmp_path / "state")
    registry.add("up", f"{remote}#main")
    registry.add("local", str(tmp_path))
    assert [entry["name"] for entry in registry.entries()] == ["local", "up"]
    assert registry.get("up") == {"name": "up", "location": remote, "ref": "main"}
    assert resolve_source("up", registry=registry).ref == "main"
    registry.remove("up")
    with pytest.raises(NotFoundError):
        registry.remove("up")
    with pytest.raises(UsageError):
        registry.add("Bad Name", str(tmp_path))


def test_sources_command(tmp_path, capsys):
    state = ["--state-dir", str(tmp_path / "state")]
    assert main([*state, "sources", "list"]) == 0
    assert "No sources registered" in capsys.readouterr().out
    assert main([*state, "sources", "add", "cb", str(tmp_path)]) == 0
    assert main([*state, "-o", "json", "sources", "list"]) == 0
    assert '"name": "cb"' in capsys.readouterr().out
    assert main([*state, "sources", "remove", "cb"]) == 0
    assert main([*state, "sources", "remove", "cb"]) == 6
