"""Reminder lifecycle: restore idempotency and task resilience."""

import asyncio
from datetime import datetime, timezone

from plugins.core import reminders as rem


def _entry(rid="r1", due_offset=3600, interval=0):
    return {
        "id": rid, "channel_id": 1, "user_id": 2, "username": "u",
        "message": "Testtermin", "mode": "notify",
        "due_ts": datetime.now(timezone.utc).timestamp() + due_offset,
        "interval_seconds": interval,
    }


def test_restore_idempotent(monkeypatch, tmp_path):
    monkeypatch.setattr(rem, "_REMINDERS_FILE", tmp_path / "reminders.json")

    async def scenario():
        rem._save([_entry()])
        rem._restore()
        assert set(rem._reminder_tasks) == {"r1"}
        first = rem._reminder_tasks["r1"]

        rem._restore()  # simulated on_ready re-entry
        assert set(rem._reminder_tasks) == {"r1"}
        second = rem._reminder_tasks["r1"]
        assert first is not second

        await asyncio.sleep(0)  # let cancellations propagate
        assert first.cancelled()

        second.cancel()
        rem._reminder_tasks.clear()

    asyncio.run(scenario())


def test_restore_drops_past_oneshot(monkeypatch, tmp_path):
    monkeypatch.setattr(rem, "_REMINDERS_FILE", tmp_path / "reminders.json")

    async def scenario():
        rem._save([_entry(rid="old", due_offset=-100)])
        rem._restore()
        assert rem._reminder_tasks == {}
        assert rem._load() == []

    asyncio.run(scenario())


def test_task_survives_fire_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(rem, "_REMINDERS_FILE", tmp_path / "reminders.json")
    calls = {"n": 0}

    async def boom(entry):
        calls["n"] += 1
        raise RuntimeError("discord down")

    monkeypatch.setattr(rem, "_fire", boom)

    async def scenario():
        entry = _entry(rid="x", due_offset=-1, interval=3600)
        rem._save([entry])
        task = asyncio.create_task(rem._task(entry))
        await asyncio.sleep(0.05)
        assert calls["n"] == 1
        assert not task.done()  # recurring task survived the exception
        assert entry["due_ts"] > datetime.now(timezone.utc).timestamp()
        task.cancel()

    asyncio.run(scenario())


def test_oneshot_dropped_after_fire_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(rem, "_REMINDERS_FILE", tmp_path / "reminders.json")

    async def boom(entry):
        raise RuntimeError("discord down")

    monkeypatch.setattr(rem, "_fire", boom)

    async def scenario():
        entry = _entry(rid="once", due_offset=-1, interval=0)
        rem._save([entry])
        await rem._task(entry)  # returns after one iteration for one-shots
        assert rem._load() == []

    asyncio.run(scenario())


def test_restore_reschedules_past_recurring(monkeypatch, tmp_path):
    monkeypatch.setattr(rem, "_REMINDERS_FILE", tmp_path / "reminders.json")

    async def scenario():
        rem._save([_entry(rid="rec", due_offset=-100, interval=3600)])
        rem._restore()
        assert set(rem._reminder_tasks) == {"rec"}
        entry = rem._load()[0]
        assert entry["due_ts"] > datetime.now(timezone.utc).timestamp()
        rem._reminder_tasks.pop("rec").cancel()

    asyncio.run(scenario())
