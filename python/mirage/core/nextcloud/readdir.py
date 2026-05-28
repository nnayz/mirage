import logging

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.cache.index import IndexCacheStore, IndexEntry
from mirage.core.nextcloud._client import (_auth, _resolve_url, parse_propfind,
                                           session)
from mirage.core.nextcloud.constants import SCOPE_ERROR
from mirage.types import PathSpec

logger = logging.getLogger(__name__)


def _strip_prefix(path: str, prefix: str) -> str:
    if prefix and path.startswith(prefix):
        return path[len(prefix):] or "/"
    return path


async def readdir(accessor: NextcloudAccessor, path: PathSpec,
                  index: IndexCacheStore) -> list[str]:
    if isinstance(path, str):
        path = PathSpec(original=path, directory=path)
    prefix = path.prefix if isinstance(path, PathSpec) else ""
    raw_path = path.directory if path.pattern else path.original
    raw_path = _strip_prefix(raw_path, prefix)

    config = accessor.config
    virtual_key = (prefix + raw_path).rstrip("/") or "/"
    listing = await index.list_dir(virtual_key)
    if listing.entries is not None:
        return listing.entries

    url = _resolve_url(config, raw_path)
    async with session(config) as s:
        async with s.request(
                "PROPFIND",
                url,
                auth=_auth(config),
                headers={
                    "Depth": "1",
                    "Content-Type": "application/xml"
                },
        ) as resp:
            if resp.status == 404:
                raise FileNotFoundError(raw_path)
            resp.raise_for_status()
            xml_text = await resp.text()

    entries = parse_propfind(xml_text, config.url.rstrip("/"))
    names: list[str] = []
    dir_keys: set[str] = set()
    sizes: dict[str, int | None] = {}

    for entry in entries:
        rel = entry["rel"]
        if rel == raw_path.rstrip("/") or rel == "/":
            continue
        key = rel if rel.startswith("/") else "/" + rel
        names.append(key)
        if entry["is_dir"]:
            dir_keys.add(key)
        else:
            sizes[key] = entry["size"]

    names = sorted(set(names))
    if len(names) > SCOPE_ERROR:
        logger.warning(
            "nextcloud readdir: %s returned %d entries (limit %d)",
            virtual_key,
            len(names),
            SCOPE_ERROR,
        )

    virtual_entries = sorted((prefix + e if prefix else e) for e in names)
    index_entries = []
    for e in names:
        name = e.rsplit("/", 1)[-1]
        if e in dir_keys:
            entry_obj = IndexEntry(id=e, name=name, resource_type="folder")
        else:
            entry_obj = IndexEntry(id=e,
                                   name=name,
                                   resource_type="file",
                                   size=sizes.get(e))
        index_entries.append((name, entry_obj))
    await index.set_dir(virtual_key, index_entries)
    return virtual_entries
