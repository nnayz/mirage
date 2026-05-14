import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from urllib.parse import unquote, urlparse

import aiohttp

DAV_NS = "DAV:"


def _dav(tag: str) -> str:
    return f"{{{DAV_NS}}}{tag}"


def _resolve_url(config, path: str) -> str:
    base = config.url.rstrip("/")
    key = path.lstrip("/")
    if key:
        return f"{base}/{key}"
    return f"{base}/"


def _auth(config) -> aiohttp.BasicAuth | None:
    if config.username and config.password:
        return aiohttp.BasicAuth(config.username, config.password)
    return None


def _timeout(config) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=config.timeout)


@asynccontextmanager
async def session(config):
    connector = None if config.verify_ssl else aiohttp.TCPConnector(ssl=False)
    async with aiohttp.ClientSession(
            connector=connector,
            timeout=_timeout(config),
    ) as s:
        yield s


def parse_propfind(xml_text: str, base_url: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    base_path = urlparse(base_url).path.rstrip("/")
    results = []
    for response in root.findall(_dav("response")):
        href_el = response.find(_dav("href"))
        if href_el is None or not href_el.text:
            continue
        href_path = urlparse(unquote(href_el.text)).path.rstrip("/")
        if not href_path.startswith(base_path):
            continue
        rel = href_path[len(base_path):]
        if not rel:
            rel = "/"

        is_dir = False
        size = None
        modified = None
        etag = None

        for propstat in response.findall(_dav("propstat")):
            status_el = propstat.find(_dav("status"))
            if status_el is not None and "200" not in (status_el.text or ""):
                continue
            prop = propstat.find(_dav("prop"))
            if prop is None:
                continue
            rt = prop.find(_dav("resourcetype"))
            if rt is not None and rt.find(_dav("collection")) is not None:
                is_dir = True
            cl = prop.find(_dav("getcontentlength"))
            if cl is not None and cl.text:
                try:
                    size = int(cl.text)
                except ValueError:
                    pass
            lm = prop.find(_dav("getlastmodified"))
            if lm is not None:
                modified = lm.text
            et = prop.find(_dav("getetag"))
            if et is not None:
                etag = (et.text or "").strip('"')

        results.append({
            "rel": rel,
            "is_dir": is_dir,
            "size": size,
            "modified": modified,
            "etag": etag,
        })
    return results
