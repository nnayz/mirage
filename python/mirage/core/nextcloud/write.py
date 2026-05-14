import time

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.core.nextcloud._client import _auth, _resolve_url, session
from mirage.observe.context import record
from mirage.types import PathSpec


async def write_bytes(accessor: NextcloudAccessor, path: PathSpec,
                      data: bytes) -> None:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    key = path.strip_prefix
    config = accessor.config
    url = _resolve_url(config, key)
    start_ms = int(time.monotonic() * 1000)
    async with session(config) as s:
        async with s.put(url, auth=_auth(config), data=data) as resp:
            resp.raise_for_status()
    record("write", key, "nextcloud", len(data), start_ms)
