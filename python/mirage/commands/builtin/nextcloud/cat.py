from collections.abc import AsyncIterator

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.commands.builtin.nextcloud._provision import file_read_provision
from mirage.commands.builtin.utils.stream import _resolve_source
from mirage.commands.registry import command
from mirage.commands.spec import SPECS
from mirage.core.nextcloud.glob import resolve_glob
from mirage.core.nextcloud.stat import stat
from mirage.core.nextcloud.stream import read_stream
from mirage.io.async_line_iterator import AsyncLineIterator
from mirage.io.cachable_iterator import CachableAsyncIterator
from mirage.io.types import ByteSource, IOResult
from mirage.types import PathSpec


async def _number_lines_stream(
        source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    num = 1
    async for line in AsyncLineIterator(source):
        yield f"     {num}\t".encode() + line + b"\n"
        num += 1


@command("cat",
         resource="nextcloud",
         spec=SPECS["cat"],
         provision=file_read_provision)
async def cat(
    accessor: NextcloudAccessor,
    paths: list[PathSpec],
    *texts: str,
    stdin: AsyncIterator[bytes] | bytes | None = None,
    n: bool = False,
    index: IndexCacheStore = None,
    **_extra: object,
) -> tuple[ByteSource | None, IOResult]:
    if paths:
        paths = await resolve_glob(accessor, paths, index)
        await stat(accessor, paths[0], index)
        source = read_stream(accessor, paths[0])
        cachable = CachableAsyncIterator(source)
        key = paths[0].strip_prefix
        io = IOResult(reads={key: cachable}, cache=[key])
        if n:
            return _number_lines_stream(cachable), io
        return cachable, io
    source = _resolve_source(stdin, "cat: missing operand")
    if n:
        return _number_lines_stream(source), IOResult()
    return source, IOResult()
