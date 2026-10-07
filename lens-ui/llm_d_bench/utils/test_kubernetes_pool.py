"""Lifecycle tests use fake clients; no external Kubernetes access."""

import asyncio
from types import SimpleNamespace

import pytest

from llm_d_bench.utils import kubernetes_pool as pool


def configuration(credential="one", **kwargs):
    return SimpleNamespace(host="https://cluster", api_key={"authorization": credential}, **kwargs)


@pytest.fixture
def clients(monkeypatch):
    created = []

    class FakeClient:
        def __init__(self, configuration):
            self.configuration = configuration
            self.closed = False
            created.append(self)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(pool.client, "ApiClient", FakeClient)
    monkeypatch.setattr(pool, "cleanup_configuration", lambda config: setattr(config, "cleaned", True))
    return created


@pytest.mark.asyncio
async def test_request_scoped_without_start(clients):
    first = configuration()
    async with pool.pooled_client(first) as api:
        assert not api.closed
    assert api.closed and first.cleaned
    async with pool.pooled_client(configuration()) as other:
        assert api is not other


@pytest.mark.asyncio
async def test_pool_reuses_equivalent_credentials_and_cleans_candidates(clients):
    await pool.start_pool()
    retained, candidate = configuration(), configuration()
    try:
        async with pool.pooled_client(retained) as first:
            pass
        assert not first.closed and not getattr(retained, "cleaned", False)
        async with pool.pooled_client(candidate) as second:
            assert second is first
            assert candidate.cleaned
    finally:
        await pool.close_pool()
    assert first.closed and retained.cleaned


@pytest.mark.asyncio
async def test_rotated_credentials_and_certificate_contents_are_separate(clients, tmp_path):
    cert = tmp_path / "cert"
    cert.write_text("first")
    await pool.start_pool()
    try:
        async with pool.pooled_client(configuration(cert_file=str(cert))) as first:
            pass
        cert.write_text("second")
        async with pool.pooled_client(configuration(cert_file=str(cert))) as second:
            assert second is not first
        async with pool.pooled_client(configuration("rotated", cert_file=str(cert))) as third:
            assert third is not second
    finally:
        await pool.close_pool()
    assert all(api.closed for api in clients)


@pytest.mark.asyncio
async def test_lru_is_bounded_and_active_clients_survive_eviction(clients, monkeypatch):
    monkeypatch.setattr(pool, "_MAX_CLIENTS", 2)
    await pool.start_pool()
    try:
        async with pool.pooled_client(configuration("active")) as active:
            async with pool.pooled_client(configuration("old")) as old:
                pass
            async with pool.pooled_client(configuration("new")) as new:
                assert old.closed
                assert not active.closed and not new.closed
                assert sum(not item.closed for item in clients) == 2
    finally:
        await pool.close_pool()


@pytest.mark.asyncio
async def test_concurrency_bound_and_cancelled_waiter_cleans_configuration(clients, monkeypatch):
    monkeypatch.setattr(pool, "_MAX_CONCURRENCY", 1)
    await pool.start_pool()
    waiting = configuration("waiting")
    entered = asyncio.Event()

    async def waiter():
        async with pool.pooled_client(waiting):
            entered.set()

    try:
        async with pool.pooled_client(configuration()):
            task = asyncio.create_task(waiter())
            await asyncio.sleep(0)
            assert not entered.is_set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert waiting.cleaned
    finally:
        await pool.close_pool()


@pytest.mark.asyncio
async def test_shutdown_waits_for_active_use(clients):
    await pool.start_pool()
    async with pool.pooled_client(configuration()) as api:
        closing = asyncio.create_task(pool.close_pool())
        await asyncio.sleep(0)
        assert not closing.done() and not api.closed
    await closing
    assert api.closed


@pytest.mark.asyncio
async def test_lazy_idle_expiration(clients, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pool.time, "monotonic", lambda: now[0])
    await pool.start_pool()
    try:
        async with pool.pooled_client(configuration()) as first:
            pass
        now[0] = 31.0
        async with pool.pooled_client(configuration()) as second:
            assert second is not first and first.closed
    finally:
        await pool.close_pool()


def test_clients_never_cross_event_loops(clients):
    loops = [asyncio.new_event_loop(), asyncio.new_event_loop()]

    async def borrow():
        await pool.start_pool()
        async with pool.pooled_client(configuration()) as api:
            return api

    try:
        first, second = [loop.run_until_complete(borrow()) for loop in loops]
        assert first is not second
        assert not first.closed and not second.closed
    finally:
        for loop in loops:
            loop.run_until_complete(pool.close_pool())
            loop.close()
    assert all(api.closed for api in clients)


@pytest.mark.asyncio
async def test_exec_temporary_files_survive_reuse_until_pool_close(clients, monkeypatch):
    import tempfile
    from pathlib import Path

    from llm_d_bench.utils.kubernetes_auth import cleanup_configuration

    monkeypatch.setattr(pool, "cleanup_configuration", cleanup_configuration)
    directories = [tempfile.TemporaryDirectory(), tempfile.TemporaryDirectory()]
    configs = []
    for directory in directories:
        certificate = Path(directory.name) / "cert"
        certificate.write_text("same certificate")
        configs.append(configuration(cert_file=str(certificate), _exec_credential_files=directory))
    await pool.start_pool()
    try:
        async with pool.pooled_client(configs[0]) as first:
            pass
        async with pool.pooled_client(configs[1]) as second:
            assert first is second
            assert Path(directories[0].name).exists()
            assert not Path(directories[1].name).exists()
    finally:
        await pool.close_pool()
    assert not Path(directories[0].name).exists()


@pytest.mark.asyncio
async def test_failed_construction_and_fingerprint_clean_configuration(clients, monkeypatch, tmp_path):
    await pool.start_pool()
    config = configuration(cert_file=str(tmp_path / "missing"))
    try:
        with pytest.raises(FileNotFoundError):
            async with pool.pooled_client(config):
                pytest.fail("missing certificate must not be borrowed")
        assert config.cleaned
        config = configuration()

        def failed_client(**kwargs):
            raise RuntimeError("construction failed")

        monkeypatch.setattr(pool.client, "ApiClient", failed_client)
        with pytest.raises(RuntimeError, match="construction failed"):
            async with pool.pooled_client(config):
                pytest.fail("construction must fail")
        assert config.cleaned
    finally:
        await pool.close_pool()


@pytest.mark.asyncio
async def test_cancelled_borrower_releases_reference(clients):
    await pool.start_pool()
    entered = asyncio.Event()

    async def borrower():
        async with pool.pooled_client(configuration()):
            entered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(borrower())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(pool.close_pool(), timeout=1)
    assert all(api.closed for api in clients)
