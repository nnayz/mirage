from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.core.nextcloud._client import _auth, _resolve_url, session
from mirage.types import PathSpec


async def truncate(accessor: NextcloudAccessor, path: PathSpec,
                   length: int) -> None:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    key = path.strip_prefix
    config = accessor.config
    url = _resolve_url(config, key)
    async with session(config) as s:
        async with s.get(url, auth=_auth(config)) as resp:
            if resp.status in (404, 409):
                data = b""
            else:
                resp.raise_for_status()
                data = await resp.read()
    result = data[:length].ljust(length, b"\0")
    async with session(config) as s:
        async with s.put(url, auth=_auth(config), data=result) as resp:
            resp.raise_for_status()
