"""Bounded, non-executing inspection of retained document and archive bytes."""
from __future__ import annotations
import gzip
import io
from pathlib import PurePosixPath
import stat
import tarfile
import zipfile
import xml.etree.ElementTree as ET

MAX_MEMBER = 8 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_MEMBERS = 1000


def capabilities():
    from importlib.metadata import version, PackageNotFoundError
    try:pdf_version=version('pypdf')
    except PackageNotFoundError:pdf_version=None
    return {'pypdf':pdf_version,'office':'stdlib-xml/1','archives':'bounded-stdlib/1',
            'images':'native PNG/JPEG/GIF/WebP, 5 MB; no OCR or raster conversion'}


def image_type(raw):
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):return 'image/png'
    if raw.startswith(b'\xff\xd8\xff'):return 'image/jpeg'
    if raw.startswith((b'GIF87a', b'GIF89a')):return 'image/gif'
    if raw.startswith(b'RIFF') and raw[8:12] == b'WEBP':return 'image/webp'
    return None


def safe_member(name):
    p = PurePosixPath(name)
    return bool(name) and not p.is_absolute() and '..' not in p.parts and '\\' not in name and '\x00' not in name


def member_bytes(raw, filename, member):
    # The same validation as the inspection path must pass before returning
    # bytes for a fixture. Never extract archive paths to the host filesystem.
    view = inspect_body(raw, filename, member=member)
    if view.get('format') != 'archive_member':
        raise ValueError('No verified archive member')
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:return archive.read(member)
    if raw.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:raw=stream.read(MAX_MEMBER+1)
        if len(raw)>MAX_MEMBER:raise ValueError('gzip exceeds inspection limit')
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:') as archive:
        return archive.extractfile(member).read(MAX_MEMBER+1)


def inspect_body(raw, filename='', *, member=None):
    """Return an explicitly labelled view. Unsupported never means missing."""
    if image_type(raw):
        return {'format':'image', 'media_type':image_type(raw), 'readability':'use view_evidence_image'}
    if raw.startswith(b'%PDF'):
        try:
            import pypdf
        except ImportError:
            return {'format':'pdf', 'readability':'retained; PDF text reader unavailable'}
        try:
            import json, subprocess, sys
            from pathlib import Path
            if len(raw)>32*1024*1024:raise ValueError('PDF exceeds inspection limit')
            result = subprocess.run([sys.executable,str(Path(__file__).with_name('pdf_reader.py'))],
                input=raw,capture_output=True,timeout=30,check=True)
            return json.loads(result.stdout)
        except Exception as exc:
            return {'format':'pdf','readability':'retained; text extraction failed','error_type':type(exc).__name__}
    if zipfile.is_zipfile(io.BytesIO(raw)):
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_MEMBERS or sum(x.file_size for x in infos) > MAX_EXPANDED:
                return {'format':'zip','readability':'archive exceeds inspection limits','members':len(infos)}
            by_name = {}
            for entry in infos:
                by_name.setdefault(entry.filename, []).append(entry)
            def allowed(entry):
                return (safe_member(entry.filename) and not entry.is_dir()
                        and len(by_name[entry.filename]) == 1
                        and not stat.S_ISLNK(entry.external_attr >> 16)
                        and not (entry.flag_bits & 1) and entry.file_size <= MAX_MEMBER
                        and entry.file_size <= max(1, entry.compress_size) * 200)
            def get(name):
                choices = by_name.get(name, [])
                if len(choices) != 1 or not allowed(choices[0]):
                    raise ValueError('Unsafe, duplicate, encrypted or oversized archive member')
                return archive.read(choices[0])
            if member is not None:
                body = get(member)
                # One level only: nested archives remain listed, not recursively expanded.
                try:text = body.decode('utf-8')
                except UnicodeDecodeError:text = None
                return {'format':'archive_member','member':member,'bytes':len(body),
                        'representation':'exact member bytes decoded as UTF-8' if text is not None and '\x00' not in text else 'binary member retained',
                        **({'text':text} if text is not None and '\x00' not in text else {})}
            suffix = PurePosixPath(filename).suffix.lower()
            if suffix in ('.docx','.pptx','.xlsx'):
                # Publish structural locations and formulas; never recompute office
                # formulas or treat cached values as fresh calculations.
                names = [x.filename for x in infos if allowed(x) and x.filename.endswith('.xml') and (
                    (suffix == '.docx' and x.filename.startswith(('word/document', 'word/header', 'word/footer', 'word/footnotes')))
                    or (suffix == '.pptx' and x.filename.startswith(('ppt/slides/slide', 'ppt/notesSlides/notesSlide')))
                    or (suffix == '.xlsx' and x.filename.startswith(('xl/workbook', 'xl/worksheets/sheet', 'xl/sharedStrings'))))]
                sections = []
                try:
                    for name in sorted(names):
                        tree = ET.fromstring(get(name))
                        if suffix == '.xlsx':
                            # XML is verbose but preserves cell IDs, shared-string
                            # indexes, formula text and cached values without guessing.
                            value = ET.tostring(tree, encoding='unicode')
                        else:
                            value = '\n'.join(''.join(node.itertext()) for node in tree.iter()
                                if node.tag.rsplit('}',1)[-1] == 't')
                        sections.append(f'[{name}]\n{value}')
                    return {'format':suffix[1:], 'representation':'document text/XML view; source bytes unchanged; layout/assets not inferred',
                            'text':'\n\n'.join(sections), 'archive_members':len(infos)}
                except (ValueError, ET.ParseError, zipfile.BadZipFile):
                    return {'format':suffix[1:], 'readability':'retained; document text extraction failed'}
            return {'format':'zip', 'members':[{'name':x.filename,'bytes':x.file_size,'readable':allowed(x)} for x in infos],
                    'note':'Read an allowed member by name; archive members are not independent trace files.'}
    if raw.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
                data = stream.read(MAX_MEMBER + 1)
            if len(data) > MAX_MEMBER:raise ValueError('gzip exceeds inspection limit')
            return _tar_or_text(data, member, 'gzip')
        except (OSError, ValueError):
            return {'format':'gzip','readability':'retained; invalid or exceeds inspection limit'}
    if PurePosixPath(filename).suffix.lower() == '.tar':
        return _tar_or_text(raw, member, 'tar')
    if member is not None:
        raise ValueError('Source is not a supported archive')
    try:
        text = raw.decode('utf-8')
        if '\x00' in text:raise UnicodeError('binary NUL')
        return {'format':'text','representation':'exact UTF-8 text','text':text}
    except UnicodeError:
        return {'format':'binary','readability':'retained; text decoding unsupported'}


def _tar_or_text(raw, member, kind):
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode='r:') as archive:
            members = []
            total = 0
            for entry in archive:
                members.append(entry); total += entry.size
                if len(members) > MAX_MEMBERS or total > MAX_EXPANDED:
                    raise ValueError('tar exceeds inspection limits')
            counts = {x.name:sum(y.name == x.name for y in members) for x in members}
            def allowed(x):return x.isfile() and safe_member(x.name) and x.size <= MAX_MEMBER and counts[x.name] == 1
            if member is not None:
                chosen = [x for x in members if x.name == member]
                if len(chosen) != 1 or not allowed(chosen[0]):raise ValueError('unsafe tar member')
                content = archive.extractfile(chosen[0]).read(MAX_MEMBER + 1)
                try:text=content.decode('utf-8')
                except UnicodeError:text=None
                return {'format':'archive_member','member':member, 'bytes':len(content),
                        **({'text':text} if text is not None and '\x00' not in text else {'readability':'binary member retained'})}
            return {'format':'tar','members':[{'name':x.name,'bytes':x.size,'readable':allowed(x)} for x in members]}
    except tarfile.ReadError:
        if kind == 'tar' or member is not None:raise ValueError('invalid tar or member')
        try:return {'format':'gzip','representation':'decompressed UTF-8 text','text':raw.decode('utf-8')}
        except UnicodeError:return {'format':'gzip','readability':'retained; binary decompressed body'}
