"""Checks every uploaded file before anything is stored (allow-list, fail closed).

- Document type must be one of the four Data Submission slots, and the extension must be allowed for it.
- The first bytes must match the extension (PDF, Office ZIP, older Office); the browser's type is ignored.
- Office ZIP files (docx / xlsx / pptx) must contain the Office structure for that format.
- Macro-enabled formats (xlsm, docm, pptm) are not accepted on Data Submission.
"""
import hashlib
import re
import zipfile
from dataclasses import dataclass
from typing import BinaryIO

DOC_TYPES = ('CI', 'Protocol', 'CDP', 'Forecast')
DOC_LABELS = {'CI': 'Competitive Intelligence', 'Protocol': 'Trial Protocol Concept Sheet',
              'CDP': 'Clinical Development Plan (CDP)', 'Forecast': 'Forecast Assumptions'}
# Same accepted formats as the slots on the React Data Submission page.
EXTENSIONS_BY_TYPE = {
    'CI': {'pdf', 'ppt', 'pptx'},
    'Protocol': {'pdf', 'doc', 'docx'},
    'CDP': {'pdf', 'doc', 'docx', 'ppt', 'pptx'},
    'Forecast': {'xls', 'xlsx', 'ppt', 'pptx'},
}
CONTENT_TYPES = {
    'pdf': 'application/pdf',
    'doc': 'application/msword',
    'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    'xls': 'application/vnd.ms-excel',
    'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    'ppt': 'application/vnd.ms-powerpoint',
    'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
}
_PDF = b'%PDF-'
_ZIP = b'PK\x03\x04'
_OLE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'  # older Office (doc / xls / ppt)
_OOXML_FOLDER = {'docx': 'word/', 'xlsx': 'xl/', 'pptx': 'ppt/'}
_MAX_ZIP_ENTRIES = 20_000
_CONTROL = re.compile(r'[\x00-\x1f\x7f]')
_CHUNK = 1024 * 1024


class FileRejected(Exception):
    """Safe to show the user."""


class FileTooBig(FileRejected):
    pass


@dataclass(frozen=True)
class CheckedFile:
    original_name: str
    ext: str
    content_type: str
    size: int
    sha256: str


def clean_name(raw: str | None) -> str:
    """The file name as shown in the UI: no folders, no control characters, at most 200 characters."""
    name = _CONTROL.sub('', (raw or '').replace('\\', '/').rsplit('/', 1)[-1]).strip()
    if not name or name in ('.', '..'):
        raise FileRejected('A file has no name.')
    if len(name) > 200:
        stem, dot, ext = name.rpartition('.')
        name = (stem[: 200 - len(ext) - 1] + dot + ext) if dot and len(ext) <= 10 else name[:200]
    return name


def _signature_ok(ext: str, head: bytes, stream: BinaryIO) -> bool:
    if ext == 'pdf':
        return head.startswith(_PDF)
    if ext in ('doc', 'xls', 'ppt'):
        return head.startswith(_OLE)
    if ext in _OOXML_FOLDER:
        if not head.startswith(_ZIP):
            return False
        try:
            with zipfile.ZipFile(stream) as z:  # reads the directory only; nothing is extracted
                names = z.namelist()
        except (zipfile.BadZipFile, OSError, ValueError):
            return False
        folder = _OOXML_FOLDER[ext]
        return (len(names) <= _MAX_ZIP_ENTRIES and '[Content_Types].xml' in names
                and any(n.startswith(folder) for n in names)
                and not any(n.lower().endswith('vbaproject.bin') for n in names))  # macros
    return False


def check_file(stream: BinaryIO, filename: str | None, doc_type: str, max_bytes: int) -> CheckedFile:
    """Validate one upload. The stream is left at position 0, ready to be stored."""
    if doc_type not in EXTENSIONS_BY_TYPE:
        raise FileRejected('Unknown document type.')
    name = clean_name(filename)
    ext = name.rsplit('.', 1)[1].lower() if '.' in name else ''
    allowed = EXTENSIONS_BY_TYPE[doc_type]
    if ext not in allowed:
        raise FileRejected(f'{name}: {DOC_LABELS[doc_type]} accepts {", ".join(sorted(allowed))} files.')

    stream.seek(0, 2)
    size = stream.tell()
    stream.seek(0)
    if size == 0:
        raise FileRejected(f'{name}: the file is empty.')
    if size > max_bytes:
        raise FileTooBig(f'{name}: files must be {max_bytes // (1024 * 1024)} MB or smaller.')

    head = stream.read(8)
    stream.seek(0)
    ok = _signature_ok(ext, head, stream)
    stream.seek(0)
    if not ok:
        raise FileRejected(f'{name}: the content is not a valid .{ext} file.')

    digest = hashlib.sha256()
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
    stream.seek(0)
    return CheckedFile(name, ext, CONTENT_TYPES[ext], size, digest.hexdigest())


def size_label(size: int) -> str:
    return f'{max(size, 1) / (1024 * 1024):.1f} MB'
