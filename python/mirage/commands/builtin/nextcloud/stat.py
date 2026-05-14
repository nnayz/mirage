import re

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.commands.builtin.nextcloud._provision import metadata_provision
from mirage.commands.registry import command
from mirage.commands.spec import SPECS
from mirage.core.nextcloud.glob import resolve_glob
from mirage.core.nextcloud.stat import stat as stat_impl
from mirage.io.types import ByteSource, IOResult
from mirage.types import FileStat, FileType, PathSpec

_FORMAT_RE = re.compile(r"%([nsFy]|.)")

_TYPE_LABELS = {
    FileType.DIRECTORY: "directory",
    FileType.TEXT: "regular file",
    FileType.BINARY: "regular file",
    FileType.JSON: "regular file",
    FileType.CSV: "regular file",
}


def _format_stat(fmt: str, s: FileStat) -> str:

    def _replace(m: re.Match) -> str:
        spec = m.group(1)
        if spec == "n":
            return s.name
        if spec == "s":
            return str(s.size if s.size is not None else 0)
        if spec == "F":
            return _TYPE_LABELS.get(
                s.type, "regular file") if s.type else "regular file"
        if spec == "y":
            return s.modified or ""
        return "?"

    return _FORMAT_RE.sub(_replace, fmt)


@command("stat",
         resource="nextcloud",
         spec=SPECS["stat"],
         provision=metadata_provision)
async def stat(
    accessor: NextcloudAccessor,
    paths: list[PathSpec],
    *texts: str,
    stdin: bytes | None = None,
    c: str | None = None,
    f: str | None = None,
    index: IndexCacheStore = None,
    **_extra: object,
) -> tuple[ByteSource | None, IOResult]:
    if not paths:
        raise ValueError("stat: missing operand")
    paths = await resolve_glob(accessor, paths, index)
    fmt = c if c is not None else f
    lines: list[str] = []
    for p in paths:
        s = await stat_impl(accessor, p)
        if fmt is not None:
            lines.append(_format_stat(fmt, s))
        else:
            lines.append(f"name={s.name} size={s.size}"
                         f" modified={s.modified}"
                         f" type={s.type.value if s.type else None}")
    return "\n".join(lines).encode(), IOResult()
