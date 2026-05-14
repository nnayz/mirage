from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.commands.registry import command
from mirage.commands.spec import SPECS
from mirage.core.nextcloud.copy import copy
from mirage.core.nextcloud.find import find as find_impl
from mirage.core.nextcloud.glob import resolve_glob
from mirage.core.nextcloud.stat import stat as stat_impl
from mirage.io.types import ByteSource, IOResult
from mirage.types import PathSpec


async def _exists(accessor: NextcloudAccessor, path: PathSpec) -> bool:
    try:
        await stat_impl(accessor, path)
        return True
    except (FileNotFoundError, ValueError, Exception):
        return False


@command("cp", resource="nextcloud", spec=SPECS["cp"], write=True)
async def cp(
    accessor: NextcloudAccessor,
    paths: list[PathSpec],
    *texts: str,
    stdin: bytes | None = None,
    r: bool = False,
    R: bool = False,
    a: bool = False,
    f: bool = False,
    n: bool = False,
    v: bool = False,
    index: IndexCacheStore = None,
    **_extra: object,
) -> tuple[ByteSource | None, IOResult]:
    if len(paths) < 2:
        raise ValueError("cp: requires src and dst")
    paths = await resolve_glob(accessor, paths, index)
    recursive = r or R or a
    verbose_lines: list[str] = []
    if recursive:
        src_base = paths[0].strip_prefix.rstrip("/")
        dst_base = paths[1].strip_prefix.rstrip("/")
        entries = await find_impl(accessor, paths[0], index=index, type="file")
        for entry in entries:
            rel = entry[len(src_base):]
            dst_str = dst_base + rel
            dst_spec = PathSpec(original=dst_str, directory=dst_str)
            if n and await _exists(accessor, dst_spec):
                continue
            src_spec = PathSpec(original=entry, directory=entry)
            await copy(accessor, src_spec, dst_spec)
            if v:
                verbose_lines.append(f"{entry} -> {dst_str}")
        writes = {dst_base + entry[len(src_base):]: b"" for entry in entries}
        output = "\n".join(verbose_lines) + "\n" if verbose_lines else None
        return output.encode() if output else None, IOResult(writes=writes)
    if n and await _exists(accessor, paths[1]):
        return None, IOResult()
    await copy(accessor, paths[0], paths[1])
    output = None
    if v:
        output = f"{paths[0].original} -> {paths[1].original}\n".encode()
    return output, IOResult(writes={paths[1].original: b""})
