from collections.abc import AsyncIterator

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.core.nextcloud._client import _auth, _resolve_url, session
from mirage.observe.context import record_stream
from mirage.types import PathSpec


async def read_stream(
    accessor: NextcloudAccessor,
    path: PathSpec,
    index: IndexCacheStore = None,
    chunk_size: int = 8192,
) -> AsyncIterator[bytes]:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    key = path.strip_prefix
    config = accessor.config
    url = _resolve_url(config, key)
    rec = record_stream("read", key, "nextcloud")
    async with session(config) as s:
        async with s.get(url, auth=_auth(config)) as resp:
            if resp.status in (404, 409):
                raise FileNotFoundError(key)
            resp.raise_for_status()
            async for chunk in resp.content.iter_chunked(chunk_size):
                if rec is not None:
                    rec.bytes += len(chunk)
                yield chunk
