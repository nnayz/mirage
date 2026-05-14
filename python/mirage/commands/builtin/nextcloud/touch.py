from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.commands.registry import command
from mirage.commands.spec import SPECS
from mirage.core.nextcloud.exists import exists
from mirage.core.nextcloud.glob import resolve_glob
from mirage.core.nextcloud.write import write_bytes
from mirage.io.types import ByteSource, IOResult
from mirage.types import PathSpec


@command("touch", resource="nextcloud", spec=SPECS["touch"], write=True)
async def touch(
    accessor: NextcloudAccessor,
    paths: list[PathSpec],
    *texts: str,
    stdin: bytes | None = None,
    c: bool = False,
    r: str | None = None,
    d: str | None = None,
    index: IndexCacheStore = None,
    **_extra: object,
) -> tuple[ByteSource | None, IOResult]:
    if not paths:
        raise ValueError("touch: missing operand")
    paths = await resolve_glob(accessor, paths, index)
    writes: dict[str, bytes] = {}
    for p in paths:
        if c:
            continue
        if not await exists(accessor, p):
            await write_bytes(accessor, p, b"")
            writes[p.original] = b""
    return None, IOResult(writes=writes)
