from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from opendal.exceptions import NotFound
from opendal.types import EntryMode

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.commands.builtin.find_eval import (FindEntry, PredNode, build_tree,
                                               keep, start_basename)
from mirage.core.nextcloud.search import (FilesSearchQuery, SearchEntry,
                                          search_files, supports_query)
from mirage.types import PathSpec


class _Metadata(Protocol):

    @property
    def mode(self) -> EntryMode:
        ...

    @property
    def content_length(self) -> int:
        ...

    @property
    def last_modified(self) -> datetime | None:
        ...


@dataclass(frozen=True, slots=True)
class _Entry:
    key: str
    name: str
    kind: str
    size: int | None
    modified: float | None
    is_empty: bool | None = None


@dataclass(frozen=True, slots=True)
class _FindOptions:
    tree: PredNode
    min_size: int | None
    max_size: int | None
    mtime_min: float | None
    mtime_max: float | None
    maxdepth: int | None
    mindepth: int | None


def _base_path(path: PathSpec) -> str:
    relative = path.mount_path.strip("/")
    return "/" + relative if relative else "/"


def _entry_from_metadata(key: str, name: str, metadata: _Metadata) -> _Entry:
    is_dir = metadata.mode == EntryMode.Dir
    modified = (metadata.last_modified.timestamp()
                if metadata.last_modified is not None else None)
    return _Entry(
        key=key,
        name=name,
        kind="d" if is_dir else "f",
        size=0 if is_dir else metadata.content_length,
        modified=modified,
    )


def _entry_from_search(entry: SearchEntry) -> _Entry:
    return _Entry(
        key=entry.key,
        name=entry.name,
        kind=entry.kind,
        size=entry.size,
        modified=entry.modified,
    )


async def _stat_entry(
    accessor: NextcloudAccessor,
    key: str,
    name: str,
) -> _Entry | None:
    operator = accessor.operator()
    if key == "/":
        try:
            metadata = await operator.stat("/")
        except NotFound:
            return _Entry(key="/", name=name, kind="d", size=0, modified=None)
        return _entry_from_metadata("/", name, metadata)
    relative = key.strip("/")
    try:
        metadata = await operator.stat(relative)
    except NotFound:
        try:
            metadata = await operator.stat(relative + "/")
        except NotFound:
            return None
    return _entry_from_metadata(key, name, metadata)


def _depth(key: str, base: str) -> int:
    if key == base:
        return 0
    base_depth = 0 if base == "/" else base.count("/")
    return key.count("/") - base_depth


def _in_scope(key: str, base: str) -> bool:
    if base == "/":
        return key.startswith("/")
    return key == base or key.startswith(base + "/")


def _matches(entry: _Entry, base: str, options: _FindOptions) -> bool:
    if not _in_scope(entry.key, base):
        raise ValueError(
            f"Nextcloud Files Search out-of-scope path: {entry.key}")
    depth = _depth(entry.key, base)
    if options.maxdepth is not None and depth > options.maxdepth:
        return False
    candidate = FindEntry(
        key=entry.key,
        name=entry.name,
        kind=entry.kind,
        depth=depth,
        is_empty=entry.is_empty,
    )
    if not keep(candidate, options.tree, options.mindepth):
        return False
    if options.min_size is not None or options.max_size is not None:
        size = 0 if entry.kind == "d" else (entry.size or 0)
        if options.min_size is not None and size < options.min_size:
            return False
        if options.max_size is not None and size > options.max_size:
            return False
    if options.mtime_min is not None or options.mtime_max is not None:
        if entry.modified is None:
            return False
        if (options.mtime_min is not None
                and entry.modified < options.mtime_min):
            return False
        if (options.mtime_max is not None
                and entry.modified > options.mtime_max):
            return False
    return True


async def _server_find(
    accessor: NextcloudAccessor,
    path: PathSpec,
    options: _FindOptions,
) -> list[str] | None:
    query = FilesSearchQuery(
        tree=options.tree,
        min_size=options.min_size,
        max_size=options.max_size,
        mtime_min=options.mtime_min,
        mtime_max=options.mtime_max,
    )
    if not supports_query(query):
        return None
    base = _base_path(path)
    start = await _stat_entry(accessor, base, start_basename(path))
    if start is None:
        return []
    if start.kind != "d":
        return None
    entries = {base: start}
    if options.maxdepth != 0:
        found = await search_files(accessor, path, query)
        if found is None:
            return None
        for entry in found:
            entries.setdefault(entry.key, _entry_from_search(entry))
    return sorted(entry.key for entry in entries.values()
                  if _matches(entry, base, options))


def _parent_entries(key: str, base: str) -> list[_Entry]:
    parents: list[_Entry] = []
    parent = key.rsplit("/", 1)[0] or "/"
    while _in_scope(parent, base):
        parents.append(
            _Entry(
                key=parent,
                name=parent.rstrip("/").rsplit("/", 1)[-1],
                kind="d",
                size=0,
                modified=None,
            ))
        if parent == base:
            break
        parent = parent.rsplit("/", 1)[0] or "/"
    return parents


async def _fill_directory_mtime(
    accessor: NextcloudAccessor,
    entry: _Entry,
) -> _Entry:
    if entry.kind != "d" or entry.modified is not None:
        return entry
    found = await _stat_entry(accessor, entry.key, entry.name)
    return found if found is not None else entry


async def _scan_find(
    accessor: NextcloudAccessor,
    path: PathSpec,
    options: _FindOptions,
) -> list[str]:
    base = _base_path(path)
    relative = path.mount_path.strip("/")
    scan_path = relative + "/" if relative else "/"
    entries: dict[str, _Entry] = {}
    nonempty_dirs: set[str] = set()
    operator = accessor.operator()
    try:
        async for raw_entry in await operator.scan(scan_path):
            raw_key = raw_entry.path
            if not raw_key:
                continue
            key = "/" + raw_key.rstrip("/").lstrip("/")
            entry = _entry_from_metadata(
                key,
                key.rsplit("/", 1)[-1],
                raw_entry.metadata,
            )
            entries[key] = entry
            for parent in _parent_entries(key, base):
                nonempty_dirs.add(parent.key)
                entries.setdefault(parent.key, parent)
    except NotFound:
        pass
    start = await _stat_entry(accessor, base, start_basename(path))
    if start is None:
        return []
    entries[base] = start
    need_mtime = (options.mtime_min is not None
                  or options.mtime_max is not None)
    results: list[str] = []
    for key in sorted(entries):
        entry = entries[key]
        if need_mtime:
            entry = await _fill_directory_mtime(accessor, entry)
        is_empty = ((entry.size or 0) == 0
                    if entry.kind == "f" else key not in nonempty_dirs)
        entry = replace(entry, is_empty=is_empty)
        if _matches(entry, base, options):
            results.append(key)
    return results


async def find(
    accessor: NextcloudAccessor,
    path: PathSpec,
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
    empty: bool = False,
    tree: PredNode | None = None,
) -> list[str]:
    predicate = tree if tree is not None else build_tree(
        name=name,
        iname=iname,
        path_pattern=path_pattern,
        type=type,
        name_exclude=name_exclude,
        or_names=or_names,
        empty=empty,
    )
    options = _FindOptions(
        tree=predicate,
        min_size=min_size,
        max_size=max_size,
        mtime_min=mtime_min,
        mtime_max=mtime_max,
        maxdepth=maxdepth,
        mindepth=mindepth,
    )
    server_results = await _server_find(accessor, path, options)
    if server_results is not None:
        return server_results
    return await _scan_find(accessor, path, options)
