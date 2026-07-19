import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from enum import StrEnum
from http import HTTPStatus
from typing import TypeAlias
from urllib.parse import unquote, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from mirage.accessor.nextcloud import NextcloudAccessor
from mirage.commands.builtin.find_eval import (And, Name, Not, Or, PredNode,
                                               TrueNode, Type)
from mirage.resource.secrets import reveal_secret
from mirage.types import FindType, PathSpec

logger = logging.getLogger(__name__)

_XmlElement: TypeAlias = ElementTree.Element


class _Namespace(StrEnum):
    DAV = "DAV:"
    OWNCLOUD = "http://owncloud.org/ns"
    SEARCHDAV = "https://github.com/icewind1991/SearchDAV/ns"


class _Comparison(StrEnum):
    EQUAL = "eq"
    GREATER_THAN_OR_EQUAL = "gte"
    LESS_THAN_OR_EQUAL = "lte"
    LIKE = "like"


class _Boolean(StrEnum):
    AND = "and"
    OR = "or"


@dataclass(frozen=True, slots=True)
class _Property:
    namespace: _Namespace
    name: str

    @property
    def tag(self) -> str:
        return f"{{{self.namespace}}}{self.name}"


@dataclass(frozen=True, slots=True)
class FilesSearchQuery:
    tree: PredNode
    min_size: int | None = None
    max_size: int | None = None
    mtime_min: float | None = None
    mtime_max: float | None = None


@dataclass(frozen=True, slots=True)
class SearchEntry:
    key: str
    name: str
    kind: FindType
    size: int | None
    modified: float | None


@dataclass(frozen=True, slots=True)
class _SearchTarget:
    endpoint: str
    resource_scope: str


@dataclass(frozen=True, slots=True)
class _CompiledPredicate:
    condition: _XmlElement | None


_DISPLAY_NAME = _Property(_Namespace.DAV, "displayname")
_RESOURCE_TYPE = _Property(_Namespace.DAV, "resourcetype")
_CONTENT_LENGTH = _Property(_Namespace.DAV, "getcontentlength")
_LAST_MODIFIED = _Property(_Namespace.DAV, "getlastmodified")
_SIZE = _Property(_Namespace.OWNCLOUD, "size")
_SELECT_PROPERTIES = (
    _DISPLAY_NAME,
    _RESOURCE_TYPE,
    _CONTENT_LENGTH,
    _LAST_MODIFIED,
    _SIZE,
)
_ORDER_PROPERTIES = (_DISPLAY_NAME, _LAST_MODIFIED, _SIZE)
_SEARCH_METHOD = "SEARCH"
_SEARCH_ENDPOINT_PATH = "/remote.php/dav/"
_SEARCH_DEPTH = "infinity"
_SEARCH_PAGE_SIZE = 100
_SEARCH_HEADERS = {
    "Accept": "application/xml",
    "Content-Type": "text/xml; charset=utf-8",
}
_UNAVAILABLE_STATUS_CODES = frozenset({
    HTTPStatus.NOT_FOUND,
    HTTPStatus.METHOD_NOT_ALLOWED,
    HTTPStatus.NOT_IMPLEMENTED,
})

ElementTree.register_namespace("d", _Namespace.DAV)
ElementTree.register_namespace("oc", _Namespace.OWNCLOUD)
ElementTree.register_namespace("sd", _Namespace.SEARCHDAV)


def _qname(namespace: _Namespace, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _dav(name: str) -> str:
    return _qname(_Namespace.DAV, name)


def _sd(name: str) -> str:
    return _qname(_Namespace.SEARCHDAV, name)


def _search_target(url: str) -> _SearchTarget | None:
    parsed = urlsplit(url)
    marker = parsed.path.find(_SEARCH_ENDPOINT_PATH)
    if marker < 0:
        return None
    dav_end = marker + len(_SEARCH_ENDPOINT_PATH)
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


def _property(parent: _XmlElement, field: _Property) -> _XmlElement:
    prop = ElementTree.SubElement(parent, _dav("prop"))
    ElementTree.SubElement(prop, field.tag)
    return prop


def _comparison(operation: _Comparison, field: _Property,
                value: str | int) -> _XmlElement:
    element = ElementTree.Element(_dav(operation))
    _property(element, field)
    literal = ElementTree.SubElement(element, _dav("literal"))
    literal.text = str(value)
    return element


def _is_collection() -> _XmlElement:
    return ElementTree.Element(_dav("is-collection"))


def _negate(condition: _XmlElement) -> _XmlElement:
    element = ElementTree.Element(_dav("not"))
    element.append(condition)
    return element


def _not_collection() -> _XmlElement:
    return _negate(_is_collection())


def _combine(operation: _Boolean, elements: list[_XmlElement]) -> _XmlElement:
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


def _compile_predicate(node: PredNode) -> _CompiledPredicate | None:
    if isinstance(node, TrueNode):
        return _CompiledPredicate(None)
    if isinstance(node, Name):
        if "[" in node.pattern:
            return None
        return _CompiledPredicate(_name_condition(node))
    if isinstance(node, Type):
        if node.kind == FindType.DIRECTORY:
            return _CompiledPredicate(_is_collection())
        if node.kind == FindType.FILE:
            return _CompiledPredicate(_not_collection())
        return None
    if isinstance(node, Not):
        compiled = _compile_predicate(node.kid)
        if compiled is None or compiled.condition is None:
            return None
        return _CompiledPredicate(_negate(compiled.condition))
    if isinstance(node, (And, Or)):
        conditions: list[_XmlElement] = []
        for kid in node.kids:
            compiled = _compile_predicate(kid)
            if compiled is None:
                return None
            if compiled.condition is None:
                if isinstance(node, Or):
                    return None
                continue
            conditions.append(compiled.condition)
        if not conditions:
            return _CompiledPredicate(None) if isinstance(node, And) else None
        operation = _Boolean.AND if isinstance(node, And) else _Boolean.OR
        return _CompiledPredicate(_combine(operation, conditions))
    return None


def _name_condition(name: Name) -> _XmlElement:
    has_wildcard = "*" in name.pattern or "?" in name.pattern
    operation = (_Comparison.LIKE
                 if has_wildcard or name.icase else _Comparison.EQUAL)
    value = (_glob_to_like(name.pattern)
             if operation == _Comparison.LIKE else name.pattern)
    return _comparison(operation, _DISPLAY_NAME, value)


def _size_condition(query: FilesSearchQuery) -> _XmlElement | None:
    bounds: list[_XmlElement] = []
    if query.min_size is not None and query.max_size == query.min_size:
        bounds.append(_comparison(_Comparison.EQUAL, _SIZE, query.min_size))
    else:
        if query.min_size is not None:
            bounds.append(
                _comparison(_Comparison.GREATER_THAN_OR_EQUAL, _SIZE,
                            query.min_size))
        if query.max_size is not None:
            bounds.append(
                _comparison(_Comparison.LESS_THAN_OR_EQUAL, _SIZE,
                            query.max_size))
    if not bounds:
        return None
    file_bounds = _combine(_Boolean.AND, [_not_collection(), *bounds])
    includes_zero = (query.min_size is None or query.min_size
                     <= 0) and (query.max_size is None or query.max_size >= 0)
    if includes_zero:
        return _combine(_Boolean.OR, [_is_collection(), file_bounds])
    return file_bounds


def _where_condition(query: FilesSearchQuery) -> _XmlElement | None:
    compiled = _compile_predicate(query.tree)
    if compiled is None:
        return None
    conditions: list[_XmlElement] = []
    if compiled.condition is not None:
        conditions.append(compiled.condition)
    size_condition = _size_condition(query)
    if size_condition is not None:
        conditions.append(size_condition)
    if query.mtime_min is not None:
        conditions.append(
            _comparison(_Comparison.GREATER_THAN_OR_EQUAL, _LAST_MODIFIED,
                        math.floor(query.mtime_min)))
    if query.mtime_max is not None:
        conditions.append(
            _comparison(_Comparison.LESS_THAN_OR_EQUAL, _LAST_MODIFIED,
                        math.ceil(query.mtime_max)))
    return _combine(_Boolean.AND, conditions) if conditions else None


def supports_query(query: FilesSearchQuery) -> bool:
    return _where_condition(query) is not None


def _order(parent: _XmlElement, field: _Property) -> None:
    order = ElementTree.SubElement(parent, _dav("order"))
    _property(order, field)
    ElementTree.SubElement(order, _dav("ascending"))


def _request_body(target: _SearchTarget, path: PathSpec,
                  query: FilesSearchQuery, offset: int) -> bytes:
    condition = _where_condition(query)
    if condition is None:
        raise ValueError("Nextcloud Files Search requires a supported query")
    root = ElementTree.Element(_dav("searchrequest"))
    basic = ElementTree.SubElement(root, _dav("basicsearch"))
    select = ElementTree.SubElement(basic, _dav("select"))
    props = ElementTree.SubElement(select, _dav("prop"))
    for field in _SELECT_PROPERTIES:
        ElementTree.SubElement(props, field.tag)
    from_element = ElementTree.SubElement(basic, _dav("from"))
    scope = ElementTree.SubElement(from_element, _dav("scope"))
    href = ElementTree.SubElement(scope, _dav("href"))
    href.text = _scope_path(target, path)
    depth = ElementTree.SubElement(scope, _dav("depth"))
    depth.text = _SEARCH_DEPTH
    where = ElementTree.SubElement(basic, _dav("where"))
    where.append(condition)
    orderby = ElementTree.SubElement(basic, _dav("orderby"))
    for field in _ORDER_PROPERTIES:
        _order(orderby, field)
    limit = ElementTree.SubElement(basic, _dav("limit"))
    count = ElementTree.SubElement(limit, _dav("nresults"))
    count.text = str(_SEARCH_PAGE_SIZE)
    first = ElementTree.SubElement(limit, _sd("firstresult"))
    first.text = str(offset)
    return ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)


def _successful_status(status: str) -> bool:
    parts = status.split()
    return len(parts) >= 2 and parts[1] == str(HTTPStatus.OK.value)


def _successful_props(response: _XmlElement) -> list[_XmlElement]:
    props: list[_XmlElement] = []
    for propstat in response.findall(_dav("propstat")):
        status = propstat.findtext(_dav("status"), "")
        prop = propstat.find(_dav("prop"))
        if _successful_status(status) and prop is not None:
            props.append(prop)
    if not props:
        raise ValueError(
            "Nextcloud Files Search result has no successful properties")
    return props


def _find_text(props: list[_XmlElement], field: _Property) -> str | None:
    for prop in props:
        value = prop.findtext(field.tag)
        if value is not None:
            return value
    return None


def _has_collection(props: list[_XmlElement]) -> bool:
    for prop in props:
        resource_type = prop.find(_RESOURCE_TYPE.tag)
        if (resource_type is not None
                and resource_type.find(_dav("collection")) is not None):
            return True
    return False


def _strip_scope(path: str, scope: str) -> str | None:
    if path == scope:
        return ""
    prefix = scope.rstrip("/") + "/"
    return path[len(prefix):] if path.startswith(prefix) else None


def _relative_path(href: str, target: _SearchTarget) -> str:
    href_path = unquote(urlsplit(href).path).rstrip("/")
    resource_scope = target.resource_scope.rstrip("/")
    relative = _strip_scope(href_path, resource_scope)
    if relative is None:
        dav_root = unquote(urlsplit(target.endpoint).path).rstrip("/")
        relative = _strip_scope(href_path, dav_root + resource_scope)
    if relative is None:
        raise ValueError(
            f"Nextcloud Files Search returned an out-of-scope href: {href}")
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


def _size(props: list[_XmlElement]) -> int | None:
    value = _find_text(props, _SIZE)
    if value is None:
        value = _find_text(props, _CONTENT_LENGTH)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(
            f"invalid Nextcloud Files Search size: {value}") from exc


def _parse_response(response: _XmlElement,
                    target: _SearchTarget) -> SearchEntry:
    href = response.findtext(_dav("href"))
    if href is None:
        raise ValueError("Nextcloud Files Search result is missing href")
    props = _successful_props(response)
    key = _relative_path(href, target)
    name = (_find_text(props, _DISPLAY_NAME)
            or key.rstrip("/").rsplit("/", 1)[-1])
    return SearchEntry(
        key=key,
        name=name,
        kind=(FindType.DIRECTORY if _has_collection(props) else FindType.FILE),
        size=_size(props),
        modified=_modified_timestamp(_find_text(props, _LAST_MODIFIED)),
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
) -> list[SearchEntry] | None:
    if not supports_query(query):
        return None
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
            headers=_SEARCH_HEADERS,
            timeout=accessor.config.timeout,
            verify=accessor.config.verify_ssl,
    ) as client:
        while True:
            response = await client.request(
                _SEARCH_METHOD,
                target.endpoint,
                content=_request_body(target, path, query, offset),
            )
            if response.status_code in _UNAVAILABLE_STATUS_CODES:
                logger.debug(
                    "Nextcloud Files Search unavailable with HTTP %d",
                    response.status_code,
                )
                return None
            response.raise_for_status()
            if response.status_code != HTTPStatus.MULTI_STATUS:
                raise ValueError("Nextcloud Files Search returned HTTP "
                                 f"{response.status_code}, expected "
                                 f"{HTTPStatus.MULTI_STATUS.value}")
            page = _parse_page(response.content, target)
            previous_count = len(entries)
            for entry in page:
                entries.setdefault(entry.key, entry)
            if page and len(entries) == previous_count:
                logger.debug(
                    "Nextcloud Files Search pagination made no progress")
                return None
            if len(page) < _SEARCH_PAGE_SIZE:
                break
            offset += len(page)
    return list(entries.values())
