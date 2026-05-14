import pytest

from mirage.core.nextcloud._client import _resolve_url, parse_propfind


def _make_config(url: str, username: str = None, password: str = None):
    from mirage.resource.nextcloud import NextcloudConfig
    return NextcloudConfig(url=url, username=username, password=password)


def test_resolve_url_root():
    config = _make_config("https://cloud.example.com/remote.php/dav/files/user/")
    url = _resolve_url(config, "/")
    assert url == "https://cloud.example.com/remote.php/dav/files/user/"


def test_resolve_url_file():
    config = _make_config("https://cloud.example.com/remote.php/dav/files/user/")
    url = _resolve_url(config, "/docs/file.txt")
    assert url == "https://cloud.example.com/remote.php/dav/files/user/docs/file.txt"


def test_resolve_url_no_trailing_slash():
    config = _make_config("https://cloud.example.com/remote.php/dav/files/user")
    url = _resolve_url(config, "/docs/")
    assert url == "https://cloud.example.com/remote.php/dav/files/user/docs/"


def test_parse_propfind_directory():
    base_url = "https://cloud.example.com/remote.php/dav/files/user"
    xml = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/user/docs/</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
        <d:getlastmodified>Mon, 01 Jan 2024 00:00:00 GMT</d:getlastmodified>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/files/user/docs/file.txt</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype/>
        <d:getcontentlength>42</d:getcontentlength>
        <d:getlastmodified>Tue, 02 Jan 2024 00:00:00 GMT</d:getlastmodified>
        <d:getetag>"abc123"</d:getetag>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""
    entries = parse_propfind(xml, base_url)
    assert len(entries) == 2
    dir_entry = next(e for e in entries if e["is_dir"])
    file_entry = next(e for e in entries if not e["is_dir"])
    assert dir_entry["rel"] == "/docs"
    assert file_entry["rel"] == "/docs/file.txt"
    assert file_entry["size"] == 42
    assert file_entry["etag"] == "abc123"
    assert file_entry["modified"] == "Tue, 02 Jan 2024 00:00:00 GMT"


def test_parse_propfind_root():
    base_url = "https://cloud.example.com/remote.php/dav/files/user"
    xml = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/user/</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype><d:collection/></d:resourcetype>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""
    entries = parse_propfind(xml, base_url)
    assert len(entries) == 1
    assert entries[0]["rel"] == "/"
    assert entries[0]["is_dir"] is True


def test_parse_propfind_url_encoded():
    base_url = "https://cloud.example.com/remote.php/dav/files/user"
    xml = """<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:">
  <d:response>
    <d:href>/remote.php/dav/files/user/my%20folder/note.txt</d:href>
    <d:propstat>
      <d:prop>
        <d:resourcetype/>
        <d:getcontentlength>100</d:getcontentlength>
      </d:prop>
      <d:status>HTTP/1.1 200 OK</d:status>
    </d:propstat>
  </d:response>
</d:multistatus>"""
    entries = parse_propfind(xml, base_url)
    assert len(entries) == 1
    assert entries[0]["rel"] == "/my folder/note.txt"
