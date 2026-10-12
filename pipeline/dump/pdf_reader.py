"""Isolated PDF text extraction with time/memory bounds supplied by the caller."""
import io
import json
import sys


def main():
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    # Linux enforces address-space limits. macOS may reject them; the parent
    # timeout and input/page/output limits apply on either platform.
    try:resource.setrlimit(resource.RLIMIT_AS, (1536*1024*1024,1536*1024*1024))
    except (ValueError,OSError):pass
    from pypdf import PdfReader
    raw=sys.stdin.buffer.read(32*1024*1024+1)
    if len(raw)>32*1024*1024:raise ValueError('PDF exceeds input limit')
    reader=PdfReader(io.BytesIO(raw),strict=True)
    if reader.is_encrypted or len(reader.pages)>300:raise ValueError('encrypted or exceeds page limit')
    parts=[];size=0;characters=0
    for i,page in enumerate(reader.pages):
        stream=page.get_contents()
        if stream is not None and len(stream.get_data())>8*1024*1024:
            raise ValueError('PDF page stream exceeds inspection limit')
        text=page.extract_text() or '';characters+=len(text.strip())
        value=f'[page {i+1}]\n{text}';size+=len(value.encode())
        if size>8*1024*1024:raise ValueError('PDF text exceeds inspection limit')
        parts.append(value)
    print(json.dumps({'format':'pdf','pages':len(parts),'text':'\n\n'.join(parts),
        'representation':'extracted text; layout and image content require separate inspection',
        'readability':'text extracted' if characters else 'no extractable text; image/layout inspection needed'}))


if __name__=='__main__':
    try:main()
    except Exception as exc:
        print(json.dumps({'format':'pdf','readability':'retained; text extraction failed',
                          'error_type':type(exc).__name__}))
