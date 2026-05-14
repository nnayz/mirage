from collections.abc import AsyncIterator

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.commands.builtin.nextcloud._provision import file_read_provision
from mirage.commands.builtin.utils.stream import _resolve_source
from mirage.commands.registry import command
from mirage.commands.spec import SPECS
from mirage.core.nextcloud.glob import resolve_glob
from mirage.core.nextcloud.read import read_bytes
from mirage.io.types import ByteSource, IOResult
from mirage.types import PathSpec


async def _wc_lines_stream(
        source: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    count = 0
    async for chunk in source:
        count += chunk.count(b"\n")
    yield str(count).encode()


@command("wc",
         resource="nextcloud",
         spec=SPECS["wc"],
         provision=file_read_provision)
async def wc(
    accessor: NextcloudAccessor,
    paths: list[PathSpec],
    *texts: str,
    stdin: AsyncIterator[bytes] | bytes | None = None,
    args_l: bool = False,
    w: bool = False,
    c: bool = False,
    m: bool = False,
    L: bool = False,
    index: IndexCacheStore = None,
    **_extra: object,
) -> tuple[ByteSource | None, IOResult]:
    if paths:
        paths = await resolve_glob(accessor, paths, index)
        data = await read_bytes(accessor, paths[0])
        text = data.decode(errors="replace")
        line_count = text.count("\n")
        word_count = len(text.split())
        byte_count = len(data)
        if L:
            max_len = max((len(ln) for ln in text.splitlines()), default=0)
            return str(max_len).encode(), IOResult()
        if args_l:
            return str(line_count).encode(), IOResult()
        if w:
            return str(word_count).encode(), IOResult()
        if m:
            return str(len(text)).encode(), IOResult()
        if c:
            return str(byte_count).encode(), IOResult()
        out = f"{line_count}\t{word_count}\t{byte_count}"
        return out.encode(), IOResult()

    source: AsyncIterator[bytes] = _resolve_source(stdin,
                                                   "wc: missing operand")
    if args_l:
        return _wc_lines_stream(source), IOResult()
    raw = b""
    async for chunk in source:
        raw += chunk
    text = raw.decode(errors="replace")
    lc = text.count("\n")
    wc_val = len(text.split())
    bc = len(raw)
    cc = len(text)
    if L:
        max_len = max((len(ln) for ln in text.splitlines()), default=0)
        return str(max_len).encode(), IOResult()
    if w:
        return str(wc_val).encode(), IOResult()
    if m:
        return str(cc).encode(), IOResult()
    if c:
        return str(bc).encode(), IOResult()
    return f"{lc}\t{wc_val}\t{bc}".encode(), IOResult()
