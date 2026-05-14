from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore
from mirage.core.nextcloud._client import _auth, _resolve_url, parse_propfind, session
from mirage.types import FileStat, FileType, PathSpec
from mirage.utils.filetype import guess_type


def _strip_prefix(path: str, prefix: str) -> str:
    if prefix and path.startswith(prefix):
        return path[len(prefix):] or "/"
    return path


async def stat(accessor: NextcloudAccessor,
               path: PathSpec,
               index: IndexCacheStore = None) -> FileStat:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    original_prefix = path.prefix if isinstance(path, PathSpec) else ""
    raw_path = path.original if isinstance(path, PathSpec) else path
    raw_path = _strip_prefix(raw_path, original_prefix)

    stripped = raw_path.strip("/")
    if not stripped:
        return FileStat(name="/", type=FileType.DIRECTORY)

    if index is not None:
        virtual_key = (original_prefix + "/" +
                       stripped if original_prefix else "/" + stripped)
        lookup = await index.get(virtual_key)
        if lookup.entry is not None:
            entry = lookup.entry
            if entry.resource_type == "folder":
                return FileStat(name=entry.name, type=FileType.DIRECTORY)
            return FileStat(
                name=entry.name,
                size=entry.size,
                type=guess_type(entry.name),
            )
        parent = virtual_key.rsplit("/", 1)[0] or "/"
        parent_listing = await index.list_dir(parent)
        if parent_listing.entries is not None:
            raise FileNotFoundError(raw_path)

    config = accessor.config
    url = _resolve_url(config, raw_path)
    async with session(config) as s:
        async with s.request(
                "PROPFIND",
                url,
                auth=_auth(config),
                headers={"Depth": "0", "Content-Type": "application/xml"},
        ) as resp:
            if resp.status == 404:
                raise FileNotFoundError(raw_path)
            resp.raise_for_status()
            xml_text = await resp.text()

    entries = parse_propfind(xml_text, config.url.rstrip("/"))
    if not entries:
        raise FileNotFoundError(raw_path)

    entry = entries[0]
    name = raw_path.rstrip("/").rsplit("/", 1)[-1] or "/"
    if entry["is_dir"]:
        return FileStat(name=name, type=FileType.DIRECTORY)
    return FileStat(
        name=name,
        size=entry["size"],
        modified=entry["modified"],
        fingerprint=entry["etag"] or None,
        type=guess_type(name),
        extra={"etag": entry["etag"] or ""},
    )
