import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import unquote, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.commands.builtin.find_eval import (And, Name, Not, Or, PredNode,
                                               TrueNode, Type)
from mirage.resource.secrets import reveal_secret
from mirage.types import PathSpec

logger = logging.getLogger(__name__)

DAV_NAMESPACE = "DAV:"
OWNCLOUD_NAMESPACE = "http://owncloud.org/ns"
SEARCHDAV_NAMESPACE = "https://github.com/icewind1991/SearchDAV/ns"
PAGE_SIZE = 100
_DAV_PATH = "/remote.php/dav/"
_UNAVAILABLE_STATUSES = frozenset({404, 405, 501})

ElementTree.register_namespace("d", DAV_NAMESPACE)
ElementTree.register_namespace("oc", OWNCLOUD_NAMESPACE)
ElementTree.register_namespace("sd", SEARCHDAV_NAMESPACE)


@dataclass(frozen=True, slots=True)
class SearchName:
    pattern: str
    case_insensitive: bool = False


@dataclass(frozen=True, slots=True)
class FilesSearchQuery:
    names: tuple[SearchName, ...] = ()
    kind: str | None = None
    min_size: int | None = None
    max_size: int | None = None
    mtime_min: float | None = None
    mtime_max: float | None = None


@dataclass(frozen=True, slots=True)
class SearchEntry:
    key: str
    name: str
    kind: str
    size: int | None
    modified: float | None


@dataclass(frozen=True, slots=True)
class _SearchTarget:
    endpoint: str
    resource_scope: str


def _qname(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _dav(name: str) -> str:
    return _qname(DAV_NAMESPACE, name)


def _oc(name: str) -> str:
    return _qname(OWNCLOUD_NAMESPACE, name)


def _sd(name: str) -> str:
    return _qname(SEARCHDAV_NAMESPACE, name)


def _search_target(url: str) -> _SearchTarget | None:
    parsed = urlsplit(url)
    marker = parsed.path.find(_DAV_PATH)
    if marker < 0:
        return None
    dav_end = marker + len(_DAV_PATH)
    relative = parsed.path[dav_end:].strip("/")
    parts = relative.split("/") if relative else []
    if len(parts) < 2 or parts[0] != "files":
        return None
    endpoint = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path[:dav_end], "", ""))
    return _SearchTarget(endpoint=endpoint,
                         resource_scope=unquote("/" + "/".join(parts)))


def _scope_path(target: _SearchTarget, path: PathSpec) -> str:
    relative = path.mount_path.strip("/")
    if not relative:
        return target.resource_scope
    return target.resource_scope.rstrip("/") + "/" + relative


def _property(parent: ElementTree.Element, namespace: str,
              name: str) -> ElementTree.Element:
    prop = ElementTree.SubElement(parent, _dav("prop"))
    ElementTree.SubElement(prop, _qname(namespace, name))
    return prop


def _comparison(operation: str, namespace: str, property_name: str,
                value: str | int) -> ElementTree.Element:
    element = ElementTree.Element(_dav(operation))
    _property(element, namespace, property_name)
    literal = ElementTree.SubElement(element, _dav("literal"))
    literal.text = str(value)
    return element


def _is_collection() -> ElementTree.Element:
    return ElementTree.Element(_dav("is-collection"))


def _not_collection() -> ElementTree.Element:
    element = ElementTree.Element(_dav("not"))
    element.append(_is_collection())
    return element


def _combine(operation: str,
             elements: list[ElementTree.Element]) -> ElementTree.Element:
    if len(elements) == 1:
        return elements[0]
    combined = ElementTree.Element(_dav(operation))
    combined.extend(elements)
    return combined


def _glob_to_like(pattern: str) -> str:
    translated: list[str] = []
    for char in pattern:
        if char == "*":
            translated.append("%")
        elif char == "?":
            translated.append("_")
        elif char == "\\":
            translated.append("%")
        else:
            translated.append(char)
    return "".join(translated)


def build_dav_condition(node: PredNode) -> ElementTree.Element | None:
    """Recursively translate a find predicate into DAV where condition.

    Returns None for unpushable parts (Path, Empty, bracket globs, etc).
    Supports And/Or/Not around Name/Type for server-side boolean logic.
    """
    if isinstance(node, TrueNode):
        return None
    if isinstance(node, Name):
        if "[" in node.pattern:
            return None
        return _name_condition(
            SearchName(pattern=node.pattern, case_insensitive=node.icase))
    if isinstance(node, Type):
        if node.kind == "d":
            return _is_collection()
        if node.kind == "f":
            return _not_collection()
        return None
    if isinstance(node, Not):
        inner = build_dav_condition(node.kid)
        if inner is None:
            return None
        el = ElementTree.Element(_dav("not"))
        el.append(inner)
        return el
    if isinstance(node, And):
        kids = [
            c for c in (build_dav_condition(k) for k in node.kids)
            if c is not None
        ]
        if not kids:
            return None
        return _combine("and", kids)
    if isinstance(node, Or):
        kids = [
            c for c in (build_dav_condition(k) for k in node.kids)
            if c is not None
        ]
        if not kids:
            return None
        return _combine("or", kids)
    # Path/Empty etc. cannot be expressed in basicsearch.
    # Client-side keep() handles them on reduced results (or full fallback).
    return None


def _name_condition(name: SearchName) -> ElementTree.Element:
    has_wildcard = "*" in name.pattern or "?" in name.pattern
    operation = "like" if has_wildcard or name.case_insensitive else "eq"
    value = _glob_to_like(
        name.pattern) if operation == "like" else name.pattern
    return _comparison(operation, DAV_NAMESPACE, "displayname", value)


def _size_condition(query: FilesSearchQuery) -> ElementTree.Element | None:
    bounds: list[ElementTree.Element] = []
    if query.min_size is not None and query.max_size == query.min_size:
        bounds.append(
            _comparison("eq", OWNCLOUD_NAMESPACE, "size", query.min_size))
    else:
        if query.min_size is not None:
            bounds.append(
                _comparison("gte", OWNCLOUD_NAMESPACE, "size", query.min_size))
        if query.max_size is not None:
            bounds.append(
                _comparison("lte", OWNCLOUD_NAMESPACE, "size", query.max_size))
    if not bounds:
        return None
    file_bounds = _combine("and", [_not_collection(), *bounds])
    return _combine("or", [_is_collection(), file_bounds])


def _where_condition(
        query: FilesSearchQuery,
        extra_condition: ElementTree.Element | None = None
) -> ElementTree.Element:
    conditions: list[ElementTree.Element] = []
    if extra_condition is not None:
        conditions.append(extra_condition)
    else:
        conditions.extend(_name_condition(name) for name in query.names)
    if query.kind == "d":
        conditions.append(_is_collection())
    elif query.kind == "f":
        conditions.append(_not_collection())
    size_condition = _size_condition(query)
    if size_condition is not None:
        conditions.append(size_condition)
    if query.mtime_min is not None:
        conditions.append(
            _comparison("gte", DAV_NAMESPACE, "getlastmodified",
                        math.floor(query.mtime_min)))
    if query.mtime_max is not None:
        conditions.append(
            _comparison("lte", DAV_NAMESPACE, "getlastmodified",
                        math.ceil(query.mtime_max)))
    if not conditions:
        raise ValueError("Nextcloud Files Search requires a predicate")
    return _combine("and", conditions)


def _order(parent: ElementTree.Element, namespace: str,
           property_name: str) -> None:
    order = ElementTree.SubElement(parent, _dav("order"))
    _property(order, namespace, property_name)
    ElementTree.SubElement(order, _dav("ascending"))


def _request_body(target: _SearchTarget,
                  path: PathSpec,
                  query: FilesSearchQuery,
                  offset: int,
                  extra_condition: ElementTree.Element | None = None) -> bytes:
    root = ElementTree.Element(_dav("searchrequest"))
    basic = ElementTree.SubElement(root, _dav("basicsearch"))
    select = ElementTree.SubElement(basic, _dav("select"))
    props = ElementTree.SubElement(select, _dav("prop"))
    ElementTree.SubElement(props, _dav("displayname"))
    ElementTree.SubElement(props, _dav("resourcetype"))
    ElementTree.SubElement(props, _dav("getcontentlength"))
    ElementTree.SubElement(props, _dav("getlastmodified"))
    ElementTree.SubElement(props, _oc("size"))
    from_element = ElementTree.SubElement(basic, _dav("from"))
    scope = ElementTree.SubElement(from_element, _dav("scope"))
    href = ElementTree.SubElement(scope, _dav("href"))
    href.text = _scope_path(target, path)
    depth = ElementTree.SubElement(scope, _dav("depth"))
    depth.text = "infinity"
    where = ElementTree.SubElement(basic, _dav("where"))
    where.append(_where_condition(query, extra_condition))
    orderby = ElementTree.SubElement(basic, _dav("orderby"))
    _order(orderby, DAV_NAMESPACE, "displayname")
    _order(orderby, DAV_NAMESPACE, "getlastmodified")
    _order(orderby, OWNCLOUD_NAMESPACE, "size")
    limit = ElementTree.SubElement(basic, _dav("limit"))
    count = ElementTree.SubElement(limit, _dav("nresults"))
    count.text = str(PAGE_SIZE)
    first = ElementTree.SubElement(limit, _sd("firstresult"))
    first.text = str(offset)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _successful_props(
        response: ElementTree.Element) -> list[ElementTree.Element]:
    props: list[ElementTree.Element] = []
    for propstat in response.findall(_dav("propstat")):
        status = propstat.findtext(_dav("status"), "")
        prop = propstat.find(_dav("prop"))
        if " 200 " in status and prop is not None:
            props.append(prop)
    if not props:
        raise ValueError(
            "Nextcloud Files Search result has no successful properties")
    return props


def _find_text(props: list[ElementTree.Element], name: str) -> str | None:
    for prop in props:
        value = prop.findtext(name)
        if value is not None:
            return value
    return None


def _has_collection(props: list[ElementTree.Element]) -> bool:
    for prop in props:
        resource_type = prop.find(_dav("resourcetype"))
        if (resource_type is not None
                and resource_type.find(_dav("collection")) is not None):
            return True
    return False


def _relative_path(href: str, target: _SearchTarget) -> str:
    href_path = unquote(urlsplit(href).path).rstrip("/")
    resource_scope = target.resource_scope.rstrip("/")
    if href_path.startswith(resource_scope):
        start = 0
    else:
        dav_root = unquote(urlsplit(target.endpoint).path).rstrip("/")
        dav_scope = dav_root + resource_scope
        start = len(dav_root) if href_path.startswith(dav_scope) else -1
    if start < 0:
        raise ValueError(
            f"Nextcloud Files Search returned an out-of-scope href: {href}")
    end = start + len(resource_scope)
    if len(href_path) > end and href_path[end] != "/":
        raise ValueError(
            f"Nextcloud Files Search returned an out-of-scope href: {href}")
    relative = href_path[end:].strip("/")
    return "/" + relative if relative else "/"


def _modified_timestamp(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        modified = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            modified = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(
                f"invalid Nextcloud Files Search timestamp: {value}") from exc
    if modified.tzinfo is None:
        modified = modified.replace(tzinfo=timezone.utc)
    return modified.timestamp()


def _size(props: list[ElementTree.Element]) -> int | None:
    value = _find_text(props, _oc("size"))
    if value is None:
        value = _find_text(props, _dav("getcontentlength"))
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"invalid Nextcloud Files Search size: {value}") from exc


def _parse_response(response: ElementTree.Element,
                    target: _SearchTarget) -> SearchEntry:
    href = response.findtext(_dav("href"))
    if href is None:
        raise ValueError("Nextcloud Files Search result is missing href")
    props = _successful_props(response)
    key = _relative_path(href, target)
    name = key.rstrip("/").rsplit("/", 1)[-1]
    return SearchEntry(
        key=key,
        name=name,
        kind="d" if _has_collection(props) else "f",
        size=_size(props),
        modified=_modified_timestamp(_find_text(props,
                                                _dav("getlastmodified"))),
    )


def _parse_page(content: bytes, target: _SearchTarget) -> list[SearchEntry]:
    root = ElementTree.fromstring(content)
    if root.tag != _dav("multistatus"):
        raise ValueError("invalid Nextcloud Files Search response")
    return [
        _parse_response(response, target)
        for response in root.findall(_dav("response"))
    ]


def _auth(accessor: NextcloudAccessor) -> httpx.BasicAuth | None:
    username = reveal_secret(accessor.config.username)
    if not username:
        return None
    password = reveal_secret(accessor.config.password) or ""
    return httpx.BasicAuth(username, password)


async def search_files(
    accessor: NextcloudAccessor,
    path: PathSpec,
    query: FilesSearchQuery,
    extra_condition: ElementTree.Element | None = None,
) -> list[SearchEntry] | None:
    target = _search_target(accessor.config.url)
    if target is None:
        logger.debug("Nextcloud Files Search unavailable for URL %s",
                     accessor.config.url)
        return None
    entries: dict[str, SearchEntry] = {}
    offset = 0
    async with httpx.AsyncClient(
            auth=_auth(accessor),
            follow_redirects=True,
            timeout=accessor.config.timeout,
            verify=accessor.config.verify_ssl,
    ) as client:
        while True:
            response = await client.request(
                "SEARCH",
                target.endpoint,
                content=_request_body(target, path, query, offset,
                                      extra_condition),
                headers={
                    "Accept": "application/xml",
                    "Content-Type": "text/xml; charset=utf-8",
                },
            )
            if response.status_code in _UNAVAILABLE_STATUSES:
                logger.debug(
                    "Nextcloud Files Search unavailable with HTTP %d",
                    response.status_code,
                )
                return None
            response.raise_for_status()
            if response.status_code != 207:
                raise ValueError("Nextcloud Files Search returned HTTP "
                                 f"{response.status_code}, expected 207")
            page = _parse_page(response.content, target)
            previous_count = len(entries)
            for entry in page:
                entries.setdefault(entry.key, entry)
            if page and len(entries) == previous_count:
                logger.debug(
                    "Nextcloud Files Search pagination made no progress")
                return None
            if len(page) < PAGE_SIZE:
                break
            offset += len(page)
    return list(entries.values())
