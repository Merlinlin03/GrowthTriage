import asyncio

from app.services.orchestrator import schedule


def test_runner_never_exceeds_two_concurrent_jobs():
    async def scenario():
        active = 0
        maximum = 0

        async def job():
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.02)
            active -= 1

        await asyncio.gather(*(schedule(job) for _ in range(6)))
        return maximum

    assert asyncio.run(scenario()) == 2
