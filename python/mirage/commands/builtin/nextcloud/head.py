from collections.abc import AsyncIterator

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.commands.builtin.nextcloud._provision import head_tail_provision
from mirage.commands.builtin.utils.stream import _resolve_source
from mirage.commands.registry import command
from mirage.commands.spec import SPECS
from mirage.core.nextcloud.glob import resolve_glob
from mirage.core.nextcloud.stream import read_stream
from mirage.io.async_line_iterator import AsyncLineIterator
from mirage.io.types import ByteSource, IOResult
from mirage.types import PathSpec


async def _head_stream(
    source: AsyncIterator[bytes],
    lines: int = 10,
    bytes_mode: int | None = None,
) -> AsyncIterator[bytes]:
    if bytes_mode is not None:
        remaining = bytes_mode
        async for chunk in source:
            if len(chunk) <= remaining:
                yield chunk
                remaining -= len(chunk)
                if remaining <= 0:
                    return
            else:
                yield chunk[:remaining]
                return
        return
    count = 0
    async for line in AsyncLineIterator(source):
        yield line + b"\n"
        count += 1
        if count >= lines:
            return


@command("head",
         resource="nextcloud",
         spec=SPECS["head"],
         provision=head_tail_provision)
async def head(
    accessor: NextcloudAccessor,
    paths: list[PathSpec],
    *texts: str,
    stdin: AsyncIterator[bytes] | bytes | None = None,
    n: str | None = None,
    c: str | None = None,
    index: IndexCacheStore = None,
    **_extra: object,
) -> tuple[ByteSource | None, IOResult]:
    lines = int(n) if n is not None else 10
    bytes_mode = int(c) if c is not None else None
    if paths:
        paths = await resolve_glob(accessor, paths, index)
        source = read_stream(accessor, paths[0])
        return _head_stream(source, lines, bytes_mode), IOResult()
    source = _resolve_source(stdin, "head: missing operand")
    return _head_stream(source, lines, bytes_mode), IOResult()
