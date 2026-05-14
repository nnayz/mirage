from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.core.nextcloud._client import _auth, _resolve_url, session
from mirage.types import PathSpec


async def mkdir(accessor: NextcloudAccessor, path: PathSpec) -> None:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    key = path.strip_prefix
    config = accessor.config
    url = _resolve_url(config, key)
    async with session(config) as s:
        async with s.request("MKCOL", url, auth=_auth(config)) as resp:
            if resp.status not in (201, 301, 405):
                resp.raise_for_status()
