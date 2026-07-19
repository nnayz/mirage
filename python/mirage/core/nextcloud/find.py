from datetime import datetime
from typing import Protocol

from opendal.exceptions import NotFound
from opendal.types import EntryMode

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.commands.builtin.find_eval import (And, FindEntry, Name, PredNode,
                                               TrueNode, Type, build_tree,
                                               emit_start_path, keep,
                                               start_basename)
from mirage.core.nextcloud.search import (FilesSearchQuery, SearchEntry,
                                          SearchName, build_dav_condition,
                                          search_files)
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


def _gather_server_predicates(
    node: PredNode,
    names: list[SearchName],
    kinds: set[str],
) -> None:
    if isinstance(node, TrueNode):
        return
    if isinstance(node, Name):
        if "[" in node.pattern:
            return
        names.append(
            SearchName(pattern=node.pattern, case_insensitive=node.icase))
        return
    if isinstance(node, Type):
        if node.kind in ("d", "f"):
            kinds.add(node.kind)
        return
    if isinstance(node, And):
        for kid in node.kids:
            _gather_server_predicates(kid, names, kinds)
        return
    # Path, Empty, Not, Or and other nodes are not pushed to the server
    # query; the full tree is still applied client-side in _matches/keep.
    return


def _server_query(
    tree: PredNode,
    min_size: int | None,
    max_size: int | None,
    mtime_min: float | None,
    mtime_max: float | None,
) -> FilesSearchQuery | None:
    names: list[SearchName] = []
    kinds: set[str] = set()
    _gather_server_predicates(tree, names, kinds)
    if len(kinds) > 1:
        return None
    kind = next(iter(kinds)) if kinds else None
    if (not names and kind is None and min_size is None and max_size is None
            and mtime_min is None and mtime_max is None):
        return None
    return FilesSearchQuery(
        names=tuple(names),
        kind=kind,
        min_size=min_size,
        max_size=max_size,
        mtime_min=mtime_min,
        mtime_max=mtime_max,
    )


def _metadata_entry(key: str, name: str, metadata: _Metadata) -> SearchEntry:
    is_dir = getattr(metadata, "mode", None) == EntryMode.Dir
    last_modified = getattr(metadata, "last_modified", None)
    modified = last_modified.timestamp() if last_modified is not None else None
    return SearchEntry(
        key=key,
        name=name,
        kind="d" if is_dir else "f",
        size=getattr(metadata, "content_length", 0) or 0,
        modified=modified,
    )


async def _start_entry(
    accessor: NextcloudAccessor,
    base: str,
    start_name: str,
) -> SearchEntry | None:
    op = accessor.operator()
    if base == "/":
        try:
            metadata = await op.stat("/")
        except NotFound:
            return SearchEntry(key="/",
                               name=start_name,
                               kind="d",
                               size=0,
                               modified=None)
        return _metadata_entry("/", start_name, metadata)
    key = base.strip("/")
    try:
        metadata = await op.stat(key)
    except NotFound:
        try:
            metadata = await op.stat(key + "/")
        except NotFound:
            return None
    return _metadata_entry(base, start_name, metadata)


def _entry_depth(path: str, base: str) -> int:
    if path == base:
        return 0
    base_depth = 0 if base == "/" else base.count("/")
    return path.count("/") - base_depth


def _in_scope(path: str, base: str) -> bool:
    if base == "/":
        return path.startswith("/")
    return path == base or path.startswith(base.rstrip("/") + "/")


def _matches(
    entry: SearchEntry,
    base: str,
    tree: PredNode,
    min_size: int | None,
    max_size: int | None,
    mtime_min: float | None,
    mtime_max: float | None,
    maxdepth: int | None,
    mindepth: int | None,
) -> bool:
    if not _in_scope(entry.key, base):
        raise ValueError(
            f"Nextcloud Files Search out-of-scope path: {entry.key}")
    depth = _entry_depth(entry.key, base)
    if maxdepth is not None and depth > maxdepth:
        return False
    find_entry = FindEntry(
        key=entry.key,
        name=entry.name,
        kind=entry.kind,
        depth=depth,
        is_empty=False if entry.kind == "d" else (entry.size or 0) == 0,
    )
    if not keep(find_entry, tree, mindepth):
        return False
    if entry.kind == "f" and (min_size is not None or max_size is not None):
        size = entry.size or 0
        if min_size is not None and size < min_size:
            return False
        if max_size is not None and size > max_size:
            return False
    if mtime_min is not None or mtime_max is not None:
        if entry.modified is None:
            return False
        if mtime_min is not None and entry.modified < mtime_min:
            return False
        if mtime_max is not None and entry.modified > mtime_max:
            return False
    return True


async def _server_find(
    accessor: NextcloudAccessor,
    path: PathSpec,
    tree: PredNode,
    min_size: int | None,
    max_size: int | None,
    mtime_min: float | None,
    mtime_max: float | None,
    maxdepth: int | None,
    mindepth: int | None,
) -> list[str] | None:
    query = _server_query(tree, min_size, max_size, mtime_min, mtime_max)
    tree_cond = build_dav_condition(tree)
    if query is None and tree_cond is None:
        return None
    base = "/" + path.mount_path.strip("/") if path.mount_path.strip(
        "/") else "/"
    start = await _start_entry(accessor, base, start_basename(path))
    if start is None:
        return []
    if start.kind != "d":
        return None
    entries: list[SearchEntry] = [start]
    if maxdepth != 0:
        found = await search_files(accessor,
                                   path,
                                   query or FilesSearchQuery(),
                                   extra_condition=tree_cond)
        if found is None:
            return None
        entries.extend(found)
    unique: dict[str, SearchEntry] = {}
    for entry in entries:
        unique.setdefault(entry.key, entry)
    return sorted(entry.key for entry in unique.values()
                  if _matches(entry, base, tree, min_size, max_size, mtime_min,
                              mtime_max, maxdepth, mindepth))


async def _scan_find(
    accessor: NextcloudAccessor,
    path: PathSpec,
    tree: PredNode,
    min_size: int | None,
    max_size: int | None,
    maxdepth: int | None,
    mtime_min: float | None,
    mtime_max: float | None,
    mindepth: int | None,
) -> list[str]:
    start_name = start_basename(path)
    target = path.mount_path
    pfx = target.strip("/")
    scan_path = pfx + "/" if pfx else "/"
    base = "/" + pfx if pfx else "/"
    base_depth = 0 if base == "/" else base.count("/")

    op = accessor.operator()
    results: list[str] = []
    seen_dirs: set[str] = set()
    saw_descendant = False
    dir_exists = False
    try:
        async for entry in await op.scan(scan_path):
            rel = entry.path
            if not rel:
                continue
            meta = entry.metadata
            is_dir = (rel.endswith("/")
                      or getattr(meta, "mode", None) == EntryMode.Dir)
            entry_path = "/" + rel.rstrip("/").lstrip("/")
            if entry_path == base:
                dir_exists = True
                continue
            saw_descendant = True
            kind = "d" if is_dir else "f"
            content_length = getattr(meta, "content_length", 0) or 0
            last_modified = getattr(meta, "last_modified", None)

            file_entries: list[tuple[str, str]] = [(entry_path, kind)]
            if not is_dir:
                parent = entry_path.rsplit("/", 1)[0] or "/"
                while parent and parent != base and parent != "/":
                    if parent not in seen_dirs:
                        seen_dirs.add(parent)
                        file_entries.append((parent, "d"))
                    parent = parent.rsplit("/", 1)[0] or "/"

            for entry_key, entry_kind in file_entries:
                entry_name = entry_key.rsplit("/", 1)[-1]
                depth = entry_key.count("/") - base_depth
                if maxdepth is not None and depth > maxdepth:
                    continue
                find_entry = FindEntry(
                    key=entry_key,
                    name=entry_name,
                    kind=entry_kind,
                    depth=depth,
                    is_empty=False
                    if entry_kind == "d" else content_length == 0,
                )
                if not keep(find_entry, tree, mindepth):
                    continue

                if min_size is not None or max_size is not None:
                    size = content_length if entry_kind == "f" else 0
                    if min_size is not None and size < min_size:
                        continue
                    if max_size is not None and size > max_size:
                        continue

                if mtime_min is not None or mtime_max is not None:
                    if last_modified is None:
                        continue
                    modified = last_modified.timestamp()
                    if mtime_min is not None and modified < mtime_min:
                        continue
                    if mtime_max is not None and modified > mtime_max:
                        continue

                results.append(entry_key)
    except NotFound:
        return []
    if saw_descendant or dir_exists:
        if mtime_min is not None or mtime_max is not None:
            start = await _start_entry(accessor, base, start_name)
            if (start is not None
                    and _matches(start, base, tree, min_size, max_size,
                                 mtime_min, mtime_max, maxdepth, mindepth)):
                results.append(base)
        else:
            emit_start_path(
                results,
                base,
                start_name,
                kind="d",
                is_empty=False,
                exists=True,
                tree=tree,
                maxdepth=maxdepth,
                mindepth=mindepth,
                min_size=min_size,
                max_size=max_size,
            )
    return sorted(set(results))


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
    if isinstance(path, str):
        path = PathSpec.from_str_path(path)
    predicate = tree if tree is not None else build_tree(
        name=name,
        iname=iname,
        path_pattern=path_pattern,
        type=type,
        name_exclude=name_exclude,
        or_names=or_names,
        empty=empty,
    )
    server_results = await _server_find(
        accessor,
        path,
        predicate,
        min_size,
        max_size,
        mtime_min,
        mtime_max,
        maxdepth,
        mindepth,
    )
    if server_results is not None:
        return server_results
    return await _scan_find(
        accessor,
        path,
        predicate,
        min_size,
        max_size,
        maxdepth,
        mtime_min,
        mtime_max,
        mindepth,
    )
