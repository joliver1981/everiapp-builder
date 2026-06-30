import pytest

from aihub_agent.ports import PortPool


@pytest.mark.asyncio
async def test_allocate_lowest_first():
    pool = PortPool(9000, 9002)
    assert await pool.allocate() == 9000
    assert await pool.allocate() == 9001
    assert await pool.allocate() == 9002


@pytest.mark.asyncio
async def test_release_returns_port():
    pool = PortPool(9000, 9001)
    a = await pool.allocate()
    b = await pool.allocate()
    await pool.release(a)
    c = await pool.allocate()
    assert c == a
    assert sorted(pool.used) == sorted([b, c])


@pytest.mark.asyncio
async def test_pool_exhaustion_raises():
    pool = PortPool(9000, 9000)
    await pool.allocate()
    with pytest.raises(RuntimeError):
        await pool.allocate()


@pytest.mark.asyncio
async def test_preferred_port_honoured_when_free():
    pool = PortPool(9000, 9010)
    p = await pool.allocate(preferred=9005)
    assert p == 9005


@pytest.mark.asyncio
async def test_preferred_port_falls_back_when_taken():
    pool = PortPool(9000, 9010)
    await pool.allocate(preferred=9005)
    p = await pool.allocate(preferred=9005)
    assert p == 9000  # falls back to lowest free
