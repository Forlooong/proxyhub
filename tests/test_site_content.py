"""Static uploads are bounded, path-safe and inert in the preview document."""

import base64
import io
import stat
import struct
import zipfile
from html.parser import HTMLParser

import pytest

from proxyhub import site_content


def encoded_zip(entries, *, compression=zipfile.ZIP_DEFLATED):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=compression) as archive:
        for name, content in entries:
            archive.writestr(name, content)
    return base64.b64encode(stream.getvalue()).decode("ascii")


def test_archive_accepts_bounded_static_bundle_and_explicit_directories():
    result = site_content.decode_archive(encoded_zip([
        ("index.html", "<h1>你好</h1>"), ("assets/", b""),
        ("assets/site.css", b"body {color:red}"), ("assets/site.js", b"console.log('hello')"),
    ]))
    assert result == {"index.html": "<h1>你好</h1>".encode(), "assets/site.css": b"body {color:red}", "assets/site.js": b"console.log('hello')"}


@pytest.mark.parametrize("name", [
    "../secret.txt", "/secret.txt", "a/../secret.txt", "a/./secret.txt", "a//secret.txt",
    "a\\secret.txt", "C:secret.txt", "a\x00secret.txt", "a\nsecret.txt", ".secret.txt",
    "a/.private/file.txt", "index.php", "index.cgi", "a.py", "a.html ",
    "/".join(["a"] * 8 + ["file.txt"]), "a" * 241 + ".txt",
])
def test_unsafe_paths_and_server_content_rejected_without_echo(name):
    with pytest.raises(ValueError) as error:
        site_content.validate_files({"index.html": b"hello", name: b"secret-content"})
    assert name not in str(error.value)
    assert "secret-content" not in str(error.value)


@pytest.mark.parametrize("extras", [
    [("INDEX.html", b"other")], [("a.txt/b.txt", b"a"), ("a.txt", b"b")],
    [("Assets/a.txt", b"a"), ("assets/b.txt", b"b")],
])
def test_case_collisions_and_file_directory_conflicts(extras):
    with pytest.raises(ValueError):
        site_content.decode_archive(encoded_zip([("index.html", b"hello"), *extras]))


def test_archive_rejects_duplicate_entries():
    with pytest.warns(UserWarning, match="Duplicate name"):
        archive = encoded_zip([("index.html", b"a"), ("index.html", b"b")])
    with pytest.raises(ValueError):
        site_content.decode_archive(archive)


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFSOCK])
def test_archive_rejects_links_and_special_files(kind):
    entry = zipfile.ZipInfo("asset.txt")
    entry.create_system = 3
    entry.external_attr = (kind | 0o644) << 16
    with pytest.raises(ValueError):
        site_content.decode_archive(encoded_zip([("index.html", b"hello"), (entry, b"target")]))


@pytest.mark.parametrize("index", [b"", b" \r\n", b"\xff", b"x" * (256 * 1024 + 1)], ids=["empty", "whitespace", "invalid-utf8", "oversize"])
def test_index_requires_bounded_nonempty_utf8(index):
    with pytest.raises(ValueError):
        site_content.validate_files({"index.html": index})


def test_bundle_limits_and_archive_bomb():
    with pytest.raises(ValueError):
        site_content.validate_files({"index.html": b"hello", "big.txt": b"x" * site_content.MAX_EXPANDED_BYTES})
    with pytest.raises(ValueError):
        site_content.decode_archive(encoded_zip([("index.html", b"hello"), ("big.txt", b"x" * (site_content.MAX_EXPANDED_BYTES + 1))]))
    with pytest.raises(ValueError):
        site_content.decode_archive(encoded_zip([("index.html", b"hello"), *[(f"a{n}/", b"") for n in range(site_content.MAX_FILES)]]))
    with pytest.raises(ValueError):
        site_content.decode_archive(base64.b64encode(b"x" * (site_content.MAX_ARCHIVE_BYTES + 1)).decode())


def test_implicit_directories_count_towards_entry_limit():
    files = {"index.html": b"hello", **{f"a{number}/b/c/d/e/f/g/file.txt": b"" for number in range(30)}}
    with pytest.raises(ValueError):
        site_content.validate_files(files)
    with pytest.raises(ValueError):
        site_content.decode_archive(encoded_zip(files.items()))


@pytest.mark.parametrize("value", [None, "not base64", "", base64.b64encode(b"not zip").decode()])
def test_invalid_archive_input(value):
    with pytest.raises(ValueError):
        site_content.decode_archive(value)


def test_crc_and_encrypted_entries_rejected():
    encoded = encoded_zip([("index.html", b"hello")], compression=zipfile.ZIP_STORED)
    broken = base64.b64decode(encoded).replace(b"hello", b"jello", 1)
    with pytest.raises(ValueError):
        site_content.decode_archive(base64.b64encode(broken).decode())
    encrypted = bytearray(base64.b64decode(encoded))
    struct.pack_into("<H", encrypted, 6, 1)
    central = encrypted.index(b"PK\x01\x02")
    struct.pack_into("<H", encrypted, central + 8, 1)
    with pytest.raises(ValueError):
        site_content.decode_archive(base64.b64encode(encrypted).decode())


class Parsed(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.tags = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_preview_embeds_relative_styles_and_images_and_blocks_active_content():
    result = site_content.preview_html({
        "index.html": b'''<html><head><base href="https://remote.test"><meta http-equiv="refresh" content="0; url=https://remote.test"><link rel="stylesheet" href="./assets/site.css?v=1"></head>
        <body onload="steal()"><h1>Hello &amp; welcome</h1><style>p {color:red}</style>
        <script>secretScript()</script><iframe srcdoc="bad">bad iframe</iframe><object>bad object</object>
        <form action="https://remote.test"><input name="secret"></form>
        <a href="https://remote.test" ping="https://remote.test" onclick="steal()">link</a>
        <img src="assets/logo.png" srcset="https://remote.test/a.png" onerror="steal()">
        <img src="https://remote.test/remote.png"><embed src="https://remote.test/x"></body></html>''',
        "assets/site.css": b"h1 {color:blue} body {background:url(https://remote.test/a.png)}",
        "assets/logo.png": b"image-bytes",
    })
    tags = Parsed(result).tags
    assert not {tag for tag, _ in tags} & {"script", "iframe", "object", "embed", "form", "base", "link"}
    assert all(not any(name.startswith("on") or name in {"href", "srcset", "ping", "action", "srcdoc"} for name in attrs) for _, attrs in tags)
    assert "secretScript" not in result
    assert "bad iframe" not in result
    assert "Hello &amp; welcome" in result
    assert "h1 {color:blue}" in result
    sources = [attrs["src"] for tag, attrs in tags if tag == "img" and "src" in attrs]
    assert sources == ["data:image/png;base64," + base64.b64encode(b"image-bytes").decode()]
    policy = next(attrs["content"] for tag, attrs in tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
    assert policy == site_content._CSP


def test_stylesheet_cannot_escape_inline_style_element():
    result = site_content.preview_html({"index.html": b'<link rel="stylesheet" href="a.css">', "a.css": b"</style><script>alert(1)</script>"})
    assert "<script>" not in result
    assert "\\3c /style>" in result


def test_preview_limit_counts_embedded_images_and_repeated_expansion():
    files = {"index.html": b'<img src="large.png"><img src="large.png">', "large.png": b"x" * (256 * 1024)}
    assert site_content.validate_files(files) == files
    with pytest.raises(ValueError, match="preview exceeds 512 KiB"):
        site_content.preview_html(files)


def test_preview_rejects_pathological_nesting_without_rejecting_content():
    files = {"index.html": b"<b>" * 130 + b"text" + b"</b>" * 130}
    assert site_content.validate_files(files) == files
    with pytest.raises(ValueError, match="preview nesting"):
        site_content.preview_html(files)
