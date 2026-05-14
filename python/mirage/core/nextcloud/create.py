from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.core.nextcloud._client import _auth, _resolve_url, session
from mirage.types import PathSpec


async def create(accessor: NextcloudAccessor, path: PathSpec) -> None:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    key = path.strip_prefix
    config = accessor.config
    url = _resolve_url(config, key)
    async with session(config) as s:
        async with s.put(url, auth=_auth(config), data=b"") as resp:
            resp.raise_for_status()
