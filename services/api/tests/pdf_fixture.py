"""Build small, real PDF files for tests: pages of text lines at given font sizes.

Uses the standard Helvetica fonts, so no font embedding is needed. Each line is
(size, text) or (size, text, "bold"); a None line leaves a paragraph gap.
"""

from __future__ import annotations

Line = tuple[float, str] | tuple[float, str, str] | None


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(pages: list[list[Line]], *, extra_catalog: str = "", encrypt: bool = False) -> bytes:
    objs: list[bytes] = []

    def add(body: str | bytes) -> int:
        objs.append(body.encode("latin-1") if isinstance(body, str) else body)
        return len(objs)

    catalog = add("")  # placeholder, filled below
    pages_id = add("")
    regular = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    bold = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")
    kids = []
    for lines in pages:
        y = 760.0
        ops = []
        for line in lines:
            if line is None:
                y -= 14
                continue
            size, text = line[0], line[1]
            font = "F2" if len(line) > 2 else "F1"
            ops.append(f"BT /{font} {size:g} Tf 72 {y:.1f} Td ({_esc(text)}) Tj ET")
            y -= size * 1.4
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        kids.append(
            add(
                f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 {regular} 0 R /F2 {bold} 0 R >> >> /Contents {content} 0 R >>"
            )
        )
    objs[catalog - 1] = f"<< /Type /Catalog /Pages {pages_id} 0 R {extra_catalog}>>".encode()
    objs[pages_id - 1] = (
        f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode()
    )
    enc_id = add("<< /Filter /Standard /V 1 /R 2 /O <00> /U <00> /P -4 >>") if encrypt else None
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    trailer = f"<< /Size {len(objs) + 1} /Root {catalog} 0 R"
    if enc_id:
        trailer += f" /Encrypt {enc_id} 0 R /ID [<01> <01>]"
    out += f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)
