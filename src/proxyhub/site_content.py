"""Bounded static website uploads and a script-free, isolated preview document."""

from __future__ import annotations

import base64
import binascii
import html
import io
import stat
import unicodedata
import zipfile
import zlib
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit


MAX_ARCHIVE_BYTES = 180 * 1024
MAX_EXPANDED_BYTES = 2 * 1024 * 1024
MAX_FILES = 200
MAX_PREVIEW_BYTES = 512 * 1024
_MAX_INDEX_BYTES = 256 * 1024
_EXTENSIONS = {
    ".html", ".htm", ".css", ".js", ".json", ".txt", ".svg", ".png",
    ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".woff", ".woff2", ".ttf", ".otf",
}
_IMAGE_TYPES = {
    ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".ico": "image/x-icon",
}
_INVALID = "Website content must contain only bounded static files with safe paths"


def _path(name: str) -> list[str]:
    if not isinstance(name, str) or not name:
        raise ValueError(_INVALID)
    try:
        if len(name.encode("utf-8")) > 240:
            raise ValueError(_INVALID)
    except UnicodeError:
        raise ValueError(_INVALID) from None
    if any(character in "\\:" or unicodedata.category(character).startswith("C") for character in name):
        raise ValueError(_INVALID)
    parts = name.split("/")
    if len(parts) > 8 or any(not part or part.startswith(".") or part.endswith((".", " ")) for part in parts):
        raise ValueError(_INVALID)
    return parts


def _register(name: str, directory: bool, paths: dict[str, tuple[str, bool]], explicit: set[str]) -> None:
    parts = _path(name)
    folded = name.casefold()
    if folded in explicit:
        raise ValueError(_INVALID)
    for index in range(1, len(parts) + 1):
        current = "/".join(parts[:index])
        is_directory = index < len(parts) or directory
        previous = paths.get(current.casefold())
        if previous is not None and previous != (current, is_directory):
            raise ValueError(_INVALID)
        paths[current.casefold()] = (current, is_directory)
        if len(paths) > MAX_FILES:
            raise ValueError(_INVALID)
    explicit.add(folded)
    if not directory and PurePosixPath(name).suffix.lower() not in _EXTENSIONS:
        raise ValueError(_INVALID)


def validate_files(files: dict[str, bytes]) -> dict[str, bytes]:
    """Validate an in-memory bundle without writing or interpreting its content."""
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
        raise ValueError(_INVALID)
    paths: dict[str, tuple[str, bool]] = {}
    explicit: set[str] = set()
    total = 0
    for name, content in files.items():
        _register(name, False, paths, explicit)
        if not isinstance(content, bytes):
            raise ValueError(_INVALID)
        total += len(content)
        if total > MAX_EXPANDED_BYTES:
            raise ValueError(_INVALID)
    index = files.get("index.html", b"")
    if not index or len(index) > _MAX_INDEX_BYTES:
        raise ValueError("Website content requires a nonempty UTF-8 root index.html of at most 256 KiB")
    try:
        if not index.decode("utf-8-sig").strip():
            raise ValueError("Website index.html must not be empty")
    except UnicodeError:
        raise ValueError("Website index.html must be valid UTF-8") from None
    return dict(files)


def decode_archive(archive_base64: str) -> dict[str, bytes]:
    """Read a small ZIP in memory, enforcing limits on actual decompressed bytes."""
    if not isinstance(archive_base64, str) or len(archive_base64) > 4 * ((MAX_ARCHIVE_BYTES + 2) // 3):
        raise ValueError("Website ZIP exceeds the 180 KiB archive limit")
    try:
        raw = base64.b64decode(archive_base64, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Website archive must be a base64-encoded ZIP") from None
    if not raw or len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError("Website ZIP exceeds the 180 KiB archive limit")
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            entries = archive.infolist()
            if not entries or len(entries) > MAX_FILES:
                raise ValueError(_INVALID)
            paths: dict[str, tuple[str, bool]] = {}
            explicit: set[str] = set()
            files: dict[str, bytes] = {}
            total = 0
            for entry in entries:
                directory = entry.is_dir()
                name = entry.orig_filename[:-1] if directory else entry.orig_filename
                _register(name, directory, paths, explicit)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.flag_bits & 1 or entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
                        or kind not in (0, stat.S_IFDIR if directory else stat.S_IFREG)
                        or (entry.external_attr & 0x10 and not directory)):
                    raise ValueError(_INVALID)
                if directory:
                    if entry.file_size != 0:
                        raise ValueError(_INVALID)
                    continue
                if entry.file_size > MAX_EXPANDED_BYTES - total:
                    raise ValueError(_INVALID)
                chunks = []
                with archive.open(entry) as stream:
                    while True:
                        chunk = stream.read(min(64 * 1024, MAX_EXPANDED_BYTES - total + 1))
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_EXPANDED_BYTES:
                            raise ValueError(_INVALID)
                        chunks.append(chunk)
                files[name] = b"".join(chunks)
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError,
            NotImplementedError, UnicodeError, zlib.error):
        raise ValueError("Website ZIP is invalid or contains unsupported entries") from None
    return validate_files(files)


_TAGS = set("a abbr address article aside b bdi bdo blockquote br caption cite code col colgroup dd del details dfn div dl dt em figcaption figure footer h1 h2 h3 h4 h5 h6 header hgroup hr i img ins kbd li main mark nav ol p pre q rp rt ruby s samp section small span strong style sub summary sup table tbody td th thead time title tr u ul var wbr".split())
_VOID = {"br", "col", "hr", "img", "wbr"}
_BLOCKED = {"script", "iframe", "object", "embed", "form", "template", "noscript", "svg", "math"}
_GLOBAL_ATTRIBUTES = {"class", "id", "title", "lang", "dir", "role", "style", "hidden"}
_ATTRIBUTES = {
    "img": {"alt", "width", "height"}, "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"}, "col": {"span"},
    "colgroup": {"span"}, "ol": {"start", "reversed", "type"},
    "li": {"value"}, "details": {"open"}, "time": {"datetime"},
}
_CSP = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'"


class _Preview(HTMLParser):
    def __init__(self, files: dict[str, bytes]):
        super().__init__(convert_charrefs=True)
        self.files = files
        self.output: list[str] = []
        self.size = 0
        self.blocked: list[str] = []
        self.open_tags: list[str] = []

    def append(self, value: str) -> None:
        self.size += len(value.encode("utf-8"))
        if self.size > MAX_PREVIEW_BYTES:
            raise ValueError("Website preview exceeds 512 KiB; the content can still be applied")
        self.output.append(value)

    def local(self, value: str | None) -> str | None:
        if not value:
            return None
        try:
            parsed = urlsplit(value)
            if parsed.scheme or parsed.netloc:
                return None
            path = unquote(parsed.path).lstrip("/")
            while path.startswith("./"):
                path = path[2:]
            _path(path)
        except (ValueError, UnicodeError):
            return None
        return path if path in self.files else None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.blocked:
            if tag not in _VOID and tag not in {"area", "base", "embed", "input", "link", "meta", "param", "source", "track"}:
                self.blocked.append(tag)
            return
        if tag in _BLOCKED:
            if tag != "embed":
                self.blocked.append(tag)
            return
        attributes = dict(attrs)
        if tag == "link":
            path = self.local(attributes.get("href"))
            if (attributes.get("rel") or "").lower() == "stylesheet" and path and path.lower().endswith(".css"):
                css = self.files[path].decode("utf-8", errors="replace").replace("<", "\\3c ")
                self.append(f"<style>{css}</style>")
            return
        if tag not in _TAGS:
            return
        rendered = []
        for name, value in attrs:
            if name in _GLOBAL_ATTRIBUTES | _ATTRIBUTES.get(tag, set()):
                rendered.append(f' {name}="{html.escape(value or "", quote=True)}"')
        if tag == "img":
            path = self.local(attributes.get("src"))
            mime = _IMAGE_TYPES.get(PurePosixPath(path).suffix.lower()) if path else None
            if path and mime:
                encoded = base64.b64encode(self.files[path]).decode("ascii")
                rendered.append(f' src="data:{mime};base64,{encoded}"')
        self.append(f"<{tag}{''.join(rendered)}>")
        if tag not in _VOID:
            if len(self.open_tags) >= 128:
                raise ValueError("Website preview nesting exceeds the supported limit; the content can still be applied")
            self.open_tags.append(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if self.blocked:
            if tag in self.blocked:
                del self.blocked[self.blocked.index(tag):]
            return
        if tag in self.open_tags:
            index = len(self.open_tags) - 1 - self.open_tags[::-1].index(tag)
            for opened in reversed(self.open_tags[index:]):
                self.append(f"</{opened}>")
            del self.open_tags[index:]

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.append(data.replace("<", "\\3c ") if self.open_tags and self.open_tags[-1] == "style" else html.escape(data, quote=False))


def preview_html(files: dict[str, bytes]) -> str:
    """Rebuild static HTML for a sandboxed iframe; scripts/navigation never survive.

    Local stylesheet links and image sources are embedded. External resources,
    CSS URL resources and sites requiring JavaScript are outside preview support.
    The caller must additionally use iframe sandbox="" and no-referrer policy.
    """
    parser = _Preview(validate_files(files))
    parser.append('<!doctype html><html><head><meta charset="utf-8">'
                  f'<meta http-equiv="Content-Security-Policy" content="{html.escape(_CSP, quote=True)}">'
                  '<meta name="referrer" content="no-referrer">'
                  '<meta name="viewport" content="width=device-width, initial-scale=1"></head><body>')
    parser.feed(files["index.html"].decode("utf-8-sig"))
    parser.close()
    for tag in reversed(parser.open_tags):
        parser.append(f"</{tag}>")
    parser.append("</body></html>")
    return "".join(parser.output)
