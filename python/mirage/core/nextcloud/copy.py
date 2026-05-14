from urllib.parse import quote, urlparse

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.core.nextcloud._client import _auth, _resolve_url, session
from mirage.types import PathSpec


def _dst_header(config, dst_key: str) -> str:
    parsed = urlparse(config.url)
    base_path = parsed.path.rstrip("/")
    dst_path = base_path + "/" + dst_key.lstrip("/")
    return f"{parsed.scheme}://{parsed.netloc}{quote(dst_path)}"


async def copy(accessor: NextcloudAccessor, src: PathSpec,
               dst: PathSpec) -> None:
    if isinstance(src, str):
        src = PathSpec(original=src, directory=src)
    if isinstance(dst, str):
        dst = PathSpec(original=dst, directory=dst)
    src_key = src.strip_prefix if isinstance(src, PathSpec) else src
    dst_key = dst.strip_prefix if isinstance(dst, PathSpec) else dst
    config = accessor.config
    url = _resolve_url(config, src_key)
    destination = _dst_header(config, dst_key)
    async with session(config) as s:
        async with s.request(
                "COPY",
                url,
                auth=_auth(config),
                headers={"Destination": destination, "Overwrite": "T"},
        ) as resp:
            if resp.status == 404:
                raise FileNotFoundError(src_key)
            resp.raise_for_status()
