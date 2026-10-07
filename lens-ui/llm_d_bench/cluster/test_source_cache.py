"""Offline source-cache integration tests using real Git repositories."""

import asyncio
import subprocess

import pytest

from llm_d_bench.cluster import repo_downloads


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("LENS_CACHE_DIR", str(tmp_path / "cache"))
    root = tmp_path / "origin"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    (root / "file").write_text("one")
    git(root, "add", ".")
    git(root, "commit", "-m", "one")
    git(root, "tag", "v1")
    monkeypatch.setitem(repo_downloads.REPO_URLS, "llm-d", str(root))
    return root


@pytest.mark.asyncio
async def test_aliases_and_concurrent_downloads_share_commit(source):
    commit = git(source, "rev-parse", "HEAD")
    paths = await asyncio.gather(
        *(
            repo_downloads.ensure_downloaded(str(i), "llm-d", ref)
            for i, ref in enumerate(["main", "v1", commit, "main"])
        )
    )
    assert len(set(paths)) == 1
    assert paths[0].name == commit
    assert (paths[0] / "file").read_text() == "one"


@pytest.mark.asyncio
async def test_moving_branch_publishes_new_checkout_preserving_old(source):
    old = await repo_downloads.ensure_downloaded("one", "llm-d", "main")
    (source / "file").write_text("two")
    git(source, "commit", "-am", "two")
    new = await repo_downloads.ensure_downloaded("two", "llm-d", "main")
    assert old != new
    assert (old / "file").read_text() == "one"
    assert (new / "file").read_text() == "two"


@pytest.mark.asyncio
async def test_distinct_urls_are_isolated(source, monkeypatch, tmp_path):
    old = await repo_downloads.ensure_downloaded("one", "llm-d", "main")
    other = tmp_path / "other"
    subprocess.check_call(["git", "clone", str(source), str(other)])
    monkeypatch.setitem(repo_downloads.REPO_URLS, "llm-d", str(other))
    new = await repo_downloads.ensure_downloaded("two", "llm-d", "main")
    assert new != old


@pytest.mark.asyncio
async def test_invalid_ref_fails_without_publishing(source):
    with pytest.raises(RuntimeError):
        await repo_downloads.ensure_downloaded("one", "llm-d", "missing")
    assert repo_downloads.get_status("one", "llm-d").state == "failed"


@pytest.mark.asyncio
async def test_ref_names_that_previously_collided_and_cached_commit_offline(source, monkeypatch):
    git(source, "branch", "feature/foo")
    original = git(source, "rev-parse", "HEAD")
    (source / "file").write_text("two")
    git(source, "commit", "-am", "two")
    git(source, "branch", "feature-foo")
    first, second = await asyncio.gather(
        repo_downloads.ensure_downloaded("one", "llm-d", "feature/foo"),
        repo_downloads.ensure_downloaded("two", "llm-d", "feature-foo"),
    )
    assert first != second
    assert (first / "file").read_text() == "one"
    source.rename(source.with_name("offline"))
    cached = await repo_downloads.ensure_downloaded("three", "llm-d", original)
    assert cached == first
