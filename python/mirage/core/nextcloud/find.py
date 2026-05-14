import fnmatch

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.core.nextcloud.readdir import readdir
from mirage.core.nextcloud.stat import stat
from mirage.types import PathSpec


async def find(
    accessor: NextcloudAccessor,
    path: PathSpec,
    index: IndexCacheStore = None,
    name: str | None = None,
    type: str | None = None,
    min_size: int | None = None,
    max_size: int | None = None,
    maxdepth: int | None = None,
    name_exclude: str | None = None,
    or_names: list[str] | None = None,
    mtime_min: float | None = None,
    mtime_max: float | None = None,
    iname: str | None = None,
    path_pattern: str | None = None,
    mindepth: int | None = None,
) -> list[str]:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    results: list[str] = []
    await _find_recursive(
        accessor,
        path,
        index,
        name=name,
        type=type,
        min_size=min_size,
        max_size=max_size,
        maxdepth=maxdepth,
        name_exclude=name_exclude,
        or_names=or_names,
        iname=iname,
        path_pattern=path_pattern,
        mindepth=mindepth,
        depth=0,
        results=results,
    )
    return sorted(results)


async def _find_recursive(
    accessor: NextcloudAccessor,
    path: PathSpec,
    index: IndexCacheStore | None,
    name: str | None,
    type: str | None,
    min_size: int | None,
    max_size: int | None,
    maxdepth: int | None,
    name_exclude: str | None,
    or_names: list[str] | None,
    iname: str | None,
    path_pattern: str | None,
    mindepth: int | None,
    depth: int,
    results: list[str],
) -> None:
    try:
        entries = await readdir(accessor, path, index)
    except (FileNotFoundError, ValueError):
        return

    for entry in entries:
        entry_spec = PathSpec(original=entry,
                              directory=entry,
                              resolved=False,
                              prefix=path.prefix)
        try:
            s = await stat(accessor, entry_spec, index)
        except (FileNotFoundError, ValueError):
            continue

        entry_name = s.name
        full_path = entry

        if mindepth is not None and depth < mindepth:
            pass
        else:
            if or_names:
                if not any(fnmatch.fnmatch(entry_name, p) for p in or_names):
                    if s.type and s.type.value != "directory":
                        continue
                else:
                    pass
            elif name and not fnmatch.fnmatch(entry_name, name):
                if s.type and s.type.value != "directory":
                    pass
                else:
                    pass

            include = True
            if or_names and not any(
                    fnmatch.fnmatch(entry_name, p) for p in or_names):
                include = False
            elif name and not fnmatch.fnmatch(entry_name, name):
                include = False
            if iname is not None and not fnmatch.fnmatch(
                    entry_name.lower(), iname.lower()):
                include = False
            if name_exclude and fnmatch.fnmatch(entry_name, name_exclude):
                include = False
            if path_pattern is not None and not fnmatch.fnmatch(
                    full_path, path_pattern):
                include = False
            if type == "file" and s.type and s.type.value == "directory":
                include = False
            if type == "directory" and s.type and s.type.value != "directory":
                include = False
            if min_size is not None and (s.size or 0) < min_size:
                include = False
            if max_size is not None and (s.size or 0) > max_size:
                include = False
            if include:
                results.append(full_path)

        if s.type and s.type.value == "directory":
            if maxdepth is None or depth < maxdepth:
                await _find_recursive(
                    accessor,
                    entry_spec,
                    index,
                    name=name,
                    type=type,
                    min_size=min_size,
                    max_size=max_size,
                    maxdepth=maxdepth,
                    name_exclude=name_exclude,
                    or_names=or_names,
                    iname=iname,
                    path_pattern=path_pattern,
                    mindepth=mindepth,
                    depth=depth + 1,
                    results=results,
                )
