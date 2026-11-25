"""Lightweight zero-dependency document text extractor for .docx / .xlsx / .pdf."""
from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET
import zipfile


def extract_document_text(path: Path, max_chars: int = 120_000) -> tuple[str, str]:
    """Return (extracted_text, format_label) or raise ValueError on unsupported/corrupt files."""
    ext = path.suffix.lower()
    if ext == '.docx':
        return _extract_docx(path, max_chars), 'docx'
    if ext == '.xlsx':
        return _extract_xlsx(path, max_chars), 'xlsx'
    if ext == '.pdf':
        return _extract_pdf(path, max_chars), 'pdf'
    raise ValueError(f'不支持的文档格式: {ext}')


def _extract_docx(path: Path, max_chars: int) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(path, 'r') as zf:
        xml_bytes = zf.read('word/document.xml')
    root = ET.fromstring(xml_bytes)
    ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
    for p in root.findall('.//w:p', ns):
        texts = [t.text for t in p.findall('.//w:t', ns) if t.text]
        if texts:
            parts.append(''.join(texts))
            if sum(len(x) for x in parts) >= max_chars:
                break
    return '\n'.join(parts)[:max_chars]


def _extract_xlsx(path: Path, max_chars: int) -> str:
    out_lines: list[str] = []
    with zipfile.ZipFile(path, 'r') as zf:
        shared: list[str] = []
        if 'xl/sharedStrings.xml' in zf.namelist():
            sroot = ET.fromstring(zf.read('xl/sharedStrings.xml'))
            for si in sroot.iter():
                if si.tag.endswith('}si') or si.tag == 'si':
                    txt = ''.join(el.text or '' for el in si.iter() if el.tag.endswith('}t') or el.tag == 't')
                    shared.append(txt)
        sheets = sorted(n for n in zf.namelist() if n.startswith('xl/worksheets/sheet') and n.endswith('.xml'))
        for s_idx, s_name in enumerate(sheets[:12], 1):
            out_lines.append(f'=== Sheet {s_idx} ({Path(s_name).stem}) ===')
            sroot = ET.fromstring(zf.read(s_name))
            for row in sroot.iter():
                if not (row.tag.endswith('}row') or row.tag == 'row'):
                    continue
                vals: list[str] = []
                for c in row:
                    if not (c.tag.endswith('}c') or c.tag == 'c'):
                        continue
                    ctype = c.attrib.get('t', '')
                    v_el = next((ch for ch in c if ch.tag.endswith('}v') or ch.tag == 'v'), None)
                    if v_el is not None and v_el.text is not None:
                        raw_v = v_el.text
                        if ctype == 's' and raw_v.isdigit() and int(raw_v) < len(shared):
                            vals.append(shared[int(raw_v)])
                        else:
                            vals.append(raw_v)
                    else:
                        inline_t = ''.join(el.text or '' for el in c.iter() if el.tag.endswith('}t') or el.tag == 't')
                        if inline_t:
                            vals.append(inline_t)
                if vals:
                    out_lines.append('\t'.join(vals))
                if sum(len(x) for x in out_lines) >= max_chars:
                    return '\n'.join(out_lines)[:max_chars]
    return '\n'.join(out_lines)[:max_chars]


def _extract_pdf(path: Path, max_chars: int) -> str:
    for mod_name in ('pypdf', 'PyPDF2'):
        try:
            mod = __import__(mod_name)
            reader = mod.PdfReader(str(path))
            chunks: list[str] = []
            for idx, page in enumerate(reader.pages, 1):
                txt = (page.extract_text() or '').strip()
                if txt:
                    chunks.append(f'--- Page {idx} ---\n{txt}')
                if sum(len(x) for x in chunks) >= max_chars:
                    break
            return '\n\n'.join(chunks)[:max_chars]
        except ImportError:
            continue
        except Exception as exc:
            raise ValueError(f'PDF 解析失败 ({mod_name}): {exc}') from None
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(str(path))
        chunks = []
        for idx, page in enumerate(doc, 1):
            txt = (page.get_text('text') or '').strip()
            if txt:
                chunks.append(f'--- Page {idx} ---\n{txt}')
            if sum(len(x) for x in chunks) >= max_chars:
                break
        doc.close()
        return '\n\n'.join(chunks)[:max_chars]
    except ImportError:
        raise ValueError('当前环境未安装 pypdf/PyMuPDF，请先在沙箱安装 pypdf 或用 python 读取 PDF') from None
