from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.core.nextcloud.readdir import readdir
from mirage.core.nextcloud.stat import stat
from mirage.types import FileType, PathSpec


async def _walk(
    accessor: NextcloudAccessor,
    path: PathSpec,
    index: IndexCacheStore | None,
    entries: list[tuple[str, int]],
) -> int:
    try:
        names = await readdir(accessor, path, index)
    except (FileNotFoundError, ValueError):
        return 0
    total = 0
    for entry in names:
        entry_spec = PathSpec(original=entry,
                              directory=entry,
                              resolved=False,
                              prefix=path.prefix)
        try:
            s = await stat(accessor, entry_spec, index)
        except (FileNotFoundError, ValueError):
            continue
        if s.type == FileType.DIRECTORY:
            sub = await _walk(accessor, entry_spec, index, entries)
            total += sub
        else:
            sz = s.size or 0
            entries.append((entry, sz))
            total += sz
    return total


async def du(accessor: NextcloudAccessor,
             path: PathSpec,
             index: IndexCacheStore | None = None) -> int:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    try:
        s = await stat(accessor, path, index)
    except (FileNotFoundError, ValueError):
        return 0
    if s.type != FileType.DIRECTORY:
        return s.size or 0
    return await _walk(accessor, path, index, [])


async def du_all(
        accessor: NextcloudAccessor,
        path: PathSpec,
        index: IndexCacheStore | None = None) -> list[tuple[str, int]]:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    entries: list[tuple[str, int]] = []
    try:
        s = await stat(accessor, path, index)
    except (FileNotFoundError, ValueError):
        return entries
    if s.type != FileType.DIRECTORY:
        sz = s.size or 0
        entries.append((path.original, sz))
        entries.append((path.original, sz))
        return entries
    total = await _walk(accessor, path, index, entries)
    entries.append((path.original, total))
    return entries
