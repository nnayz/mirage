import time

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.core.nextcloud._client import _auth, _resolve_url, session
from mirage.observe.context import record
from mirage.types import PathSpec


def _strip_prefix(path: str, prefix: str) -> str:
    if prefix and path.startswith(prefix):
        return path[len(prefix):] or "/"
    return path


async def read_bytes(accessor: NextcloudAccessor,
                     path: PathSpec,
                     index: IndexCacheStore = None,
                     offset: int = 0,
                     size: int | None = None) -> bytes:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    prefix = path.prefix if isinstance(path, PathSpec) else ""
    raw_path = path.original if isinstance(path, PathSpec) else path
    raw_path = _strip_prefix(raw_path, prefix)

    config = accessor.config
    url = _resolve_url(config, raw_path)
    headers: dict = {}
    if offset or size is not None:
        end = (offset + size - 1) if size is not None else ""
        headers["Range"] = f"bytes={offset}-{end}"

    start_ms = int(time.monotonic() * 1000)
    async with session(config) as s:
        async with s.get(url, auth=_auth(config), headers=headers) as resp:
            if resp.status in (404, 409):
                raise FileNotFoundError(raw_path)
            resp.raise_for_status()
            data = await resp.read()
    record("read", raw_path, "nextcloud", len(data), start_ms)
    return data
