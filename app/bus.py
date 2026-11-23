import asyncio

_subscribers: set[asyncio.Queue] = set()


def subscribe() -> asyncio.Queue:
    q: asyncio.Queue = asyncio.Queue()
    _subscribers.add(q)
    return q


def unsubscribe(q: asyncio.Queue) -> None:
    _subscribers.discard(q)


# Not thread-safe: call only from async handlers running on the event loop.
def publish(event: dict) -> None:
    for q in list(_subscribers):
        q.put_nowait(event)
