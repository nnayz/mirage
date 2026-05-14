from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.commands.registry import command
from mirage.commands.spec import SPECS
from mirage.core.nextcloud.glob import resolve_glob
from mirage.core.nextcloud.rename import rename
from mirage.core.nextcloud.stat import stat as stat_impl
from mirage.io.types import ByteSource, IOResult
from mirage.types import PathSpec


async def _exists(accessor: NextcloudAccessor, path: PathSpec) -> bool:
    try:
        await stat_impl(accessor, path)
        return True
    except (FileNotFoundError, ValueError, Exception):
        return False


@command("mv", resource="nextcloud", spec=SPECS["mv"], write=True)
async def mv(
    accessor: NextcloudAccessor,
    paths: list[PathSpec],
    *texts: str,
    stdin: bytes | None = None,
    f: bool = False,
    n: bool = False,
    v: bool = False,
    index: IndexCacheStore = None,
    **_extra: object,
) -> tuple[ByteSource | None, IOResult]:
    if len(paths) < 2:
        raise ValueError("mv: requires src and dst")
    paths = await resolve_glob(accessor, paths, index)
    if n and await _exists(accessor, paths[1]):
        return None, IOResult()
    await rename(accessor, paths[0], paths[1])
    writes = {
        paths[0].original: b"",
        paths[1].original: b"",
    }
    output = None
    if v:
        output = f"'{paths[0].original}' -> '{paths[1].original}'\n".encode()
    return output, IOResult(writes=writes)
