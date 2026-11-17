"""Regression tests for lost bio fallback and unknown/stale profile results.

Run: PYTHONPATH=. python -m pytest -q tests/test_profile_fetch_recovery.py
No credentials, real network, or runtime storage required.
"""
import asyncio
import time
from types import SimpleNamespace

import pytest

from modules import bio_link_service as bio
from modules.profile_guard import service


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    service.clear()
    monkeypatch.setattr(service, "_locks", {})
    monkeypatch.setattr(service, "_semaphore", None)
    monkeypatch.setattr(bio, "_inflight", set())
    monkeypatch.setattr(bio, "_pending", {})
    monkeypatch.setattr(bio, "_retry_after", {})
    monkeypatch.setattr(bio, "_semaphore", None)
    yield
    service.clear()


def test_inline_timeout_really_runs_background_check(monkeypatch):
    async def scenario():
        done = asyncio.Event()
        calls = []

        async def check(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                await asyncio.Event().wait()
            done.set()
            return {"status": "ok"}

        monkeypatch.setattr(bio, "check_user", check)
        monkeypatch.setattr(bio, "check_on_message", lambda: True)
        monkeypatch.setattr(bio, "needs_check", lambda *a: True)
        monkeypatch.setattr(bio, "await_timeout", lambda: 0.001)
        monkeypatch.setattr(bio, "rpc_delay", lambda: 0)
        monkeypatch.setattr(bio.bio_link_storage, "is_enabled", lambda *a: True)
        monkeypatch.setattr(bio.bio_link_storage, "get_user", lambda *a: None)
        await bio.ensure_user_now(SimpleNamespace(), 10, SimpleNamespace(id=20))
        await asyncio.wait_for(done.wait(), timeout=1)
        await asyncio.sleep(0)
        assert len(calls) == 2
        assert not bio._inflight
        assert not bio._pending

    asyncio.run(scenario())


@pytest.mark.parametrize("force", [False, True])
def test_fetch_failure_does_not_return_old_evidence(monkeypatch, force):
    async def scenario():
        service._cache["20"] = {"status": "ok", "about": "old", "ts": time.time() - 1000}

        async def fail(*a, **kw):
            return None

        monkeypatch.setattr(service, "_fetch", fail)
        assert await service.snapshot(None, SimpleNamespace(id=20), force=force) is None
        assert service._retry_after["20"] > time.time()

    asyncio.run(scenario())


def test_cooldown_does_not_return_stale_cache():
    async def scenario():
        service._cache["20"] = {"status": "ok", "ts": time.time() - 1000}
        service._retry_after["20"] = time.time() + 100
        assert await service.snapshot(None, SimpleNamespace(id=20)) is None

    asyncio.run(scenario())


def test_concurrent_waiters_respect_failure_cooldown(monkeypatch):
    async def scenario():
        calls = []

        async def fail(*a, **kw):
            calls.append(1)
            await asyncio.sleep(0.005)
            return None

        monkeypatch.setattr(service, "_fetch", fail)
        results = await asyncio.gather(*[
            service.snapshot(None, SimpleNamespace(id=20)) for _ in range(10)
        ])
        assert results == [None] * 10
        assert len(calls) == 1

    asyncio.run(scenario())


def test_inaccessible_response_not_cached_as_success(monkeypatch):
    async def scenario():
        async def inaccessible(*a, **kw):
            return {"status": "no_access", "ts": time.time()}

        monkeypatch.setattr(service, "_fetch", inaccessible)
        assert await service.snapshot(None, SimpleNamespace(id=20)) is None
        assert "20" not in service._cache
        assert "20" in service._retry_after

    asyncio.run(scenario())


def test_no_access_is_retryable_after_cooldown(monkeypatch):
    monkeypatch.setattr(bio.bio_link_storage, "get_user", lambda *a: {
        "status": "no_access", "checked_at": time.time()
    })
    assert bio.needs_check(10, 20)


def test_default_profile_retry_is_short(monkeypatch):
    monkeypatch.delenv("PROFILE_GUARD_ERROR_RETRY", raising=False)
    assert service.error_retry_seconds() == 30
    monkeypatch.setenv("PROFILE_GUARD_ERROR_RETRY", "120")
    assert service.error_retry_seconds() == 120


def test_fresh_success_still_cached(monkeypatch):
    async def scenario():
        calls = []
        expected = {"status": "ok", "about": "new", "ts": time.time()}

        async def fetch(*a, **kw):
            calls.append(1)
            return expected

        monkeypatch.setattr(service, "_fetch", fetch)
        assert await service.snapshot(None, SimpleNamespace(id=20)) == expected
        assert await service.snapshot(None, SimpleNamespace(id=20)) == expected
        assert len(calls) == 1

    asyncio.run(scenario())


def test_recovery_after_retry_deadline(monkeypatch):
    async def scenario():
        expected = {"status": "ok", "about": "recovered", "ts": time.time()}

        async def fetch(*a, **kw):
            return expected

        service._retry_after["20"] = time.time() - 1
        monkeypatch.setattr(service, "_fetch", fetch)
        assert await service.snapshot(None, SimpleNamespace(id=20)) == expected
        assert "20" not in service._retry_after

    asyncio.run(scenario())
