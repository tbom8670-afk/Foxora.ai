"""Urgent command text replies pass delete backlog without starving deletes."""
from __future__ import annotations

import asyncio

from modules.rpc_governor import P0_CRITICAL, P1_DELETE, RpcAdmission, RpcGovernor


def test_reply_beats_delete_but_delete_runs_after_bounded_reply_burst():
    async def scenario():
        governor = RpcGovernor(
            total_limit=1,
            noncritical_limit=1,
            delete_limit=1,
            send_limit=1,
            heavy_limit=1,
            critical_reserve=0,
        )
        held = await governor.acquire(
            RpcAdmission(P0_CRITICAL, "critical", "EditBannedRequest", "hold")
        )
        delete_task = asyncio.create_task(governor.acquire(
            RpcAdmission(P1_DELETE, "delete", "DeleteMessagesRequest", "chat")
        ))
        reply_tasks = [asyncio.create_task(governor.acquire(
            RpcAdmission(P1_DELETE, "urgent_send", "SendMessageRequest", "chat")
        )) for _ in range(4)]
        await asyncio.sleep(0)
        held.release()

        order = []
        for index in range(3):
            permit = await reply_tasks[index]
            order.append(f"reply{index + 1}")
            permit.release()
        delete_permit = await delete_task
        order.append("delete")
        delete_permit.release()
        final_reply = await reply_tasks[3]
        order.append("reply4")
        final_reply.release()

        assert order == ["reply1", "reply2", "reply3", "delete", "reply4"]
        assert governor.active == 0

    asyncio.run(scenario())


def test_urgent_reply_does_not_create_an_extra_send_slot():
    async def scenario():
        governor = RpcGovernor(
            total_limit=2, noncritical_limit=2, delete_limit=1,
            send_limit=1, heavy_limit=1, critical_reserve=0,
        )
        ordinary = await governor.acquire(
            RpcAdmission(2, "send", "SendMessageRequest", "first")
        )
        urgent_task = asyncio.create_task(governor.acquire(
            RpcAdmission(P1_DELETE, "urgent_send", "SendMessageRequest", "second")
        ))
        await asyncio.sleep(0)
        assert not urgent_task.done()
        ordinary.release()
        urgent = await urgent_task
        urgent.release()
        assert governor.active == 0

    asyncio.run(scenario())
