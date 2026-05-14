from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.commands.registry import command
from mirage.commands.spec import SPECS
from mirage.core.nextcloud.glob import resolve_glob
from mirage.core.nextcloud.mkdir import mkdir as mkdir_impl
from mirage.io.types import ByteSource, IOResult
from mirage.types import PathSpec


@command("mkdir", resource="nextcloud", spec=SPECS["mkdir"], write=True)
async def mkdir(
    accessor: NextcloudAccessor,
    paths: list[PathSpec],
    *texts: str,
    stdin: bytes | None = None,
    p: bool = False,
    v: bool = False,
    index: IndexCacheStore = None,
    **_extra: object,
) -> tuple[ByteSource | None, IOResult]:
    if not paths:
        raise ValueError("mkdir: missing operand")
    paths = await resolve_glob(accessor, paths, index)
    lines: list[str] = []
    writes: dict[str, bytes] = {}
    for path in paths:
        await mkdir_impl(accessor, path)
        writes[path.original] = b""
        if v:
            lines.append(f"mkdir: created directory '{path.original}'")
    output = ("\n".join(lines) + "\n").encode() if lines else None
    return output, IOResult(writes=writes)
