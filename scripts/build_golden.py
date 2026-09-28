#!/usr/bin/env python3
"""
Independent golden-byte generator for the multipart/form-data receiver.

These vectors are produced WITHOUT any code from the TypeScript implementation
under test: bytes are concatenated by hand here and expected digests come from
Python's hashlib over the literal part contents. The generated .bin files and
golden-manifest.json are committed so the Node test suite never needs Python.

Two sections are emitted:
  "positive" vectors -> fully valid messages with exact expected parse results
  "negative" vectors -> invalid bytes with the exact expected error code/class

Run: python3 scripts/build_golden.py [out_dir]
"""

import hashlib
import json
import os
import sys


def part(name, body, filename=None, filename_star=None, content_type=None,
         disposition_extra=None, raw_headers=None):
    """Build one part (header block + body) as bytes. No trailing CRLF here."""
    lines = []
    disp = 'Content-Disposition: form-data; name="%s"' % name
    if filename is not None:
        disp += '; filename="%s"' % filename
    if filename_star is not None:
        disp += "; filename*=UTF-8''%s" % filename_star
    if disposition_extra:
        disp += disposition_extra
    lines.append(disp)
    if content_type is not None:
        lines.append('Content-Type: %s' % content_type)
    if raw_headers is not None:
        lines = raw_headers
    head = '\r\n'.join(lines).encode('utf-8')
    if isinstance(body, str):
        body = body.encode('utf-8')
    return head, body


def message(boundary, parts, terminate=True, raw_tail=b''):
    """Concatenate a full multipart entity. `parts` items are (head, body)."""
    out = b'--' + boundary.encode()
    blocks = []
    for head, body in parts:
        blocks.append(b'\r\n' + head + b'\r\n\r\n' + body)
    out += (b'\r\n--' + boundary.encode()).join(blocks)
    if terminate:
        out += b'\r\n--' + boundary.encode() + b'--\r\n'
    out += raw_tail
    # Recompute header bytes exactly the way the wire format lays them out.
    return out


def sha(b):
    return hashlib.sha256(b).hexdigest()


PNG_1X1 = bytes.fromhex(
    '89504e470d0a1a0a0000000d4948445200000001000000010806000000'
    '1f15c4890000000d49444154789c6360000002000100ffff0300000600'
    '0557bfabd40000000049454e44ae426082'
)


def build():
    vectors = []
    negatives = []

    # ---- V1: one field + one PNG file --------------------------------------
    b1 = '----WebKitBoundary7MA4YWxkTrZu0gW'
    title = 'hello world'
    p1h, p1b = part('title', title)
    p2h, p2b = part('asset', PNG_1X1, filename='pixel.png',
                    content_type='image/png')
    bin1 = message(b1, [(p1h, p1b), (p2h, p2b)])
    vectors.append({
        'id': 'v1-field-and-png',
        'boundary': b1,
        'file': 'v1.bin',
        'parts': [
            {'index': 1, 'name': 'title', 'isFile': False, 'filename': None,
             'contentType': None, 'size': len(p1b), 'sha256': sha(p1b),
             'value': title},
            {'index': 2, 'name': 'asset', 'isFile': True, 'filename': 'pixel.png',
             'contentType': 'image/png', 'size': len(p2b), 'sha256': sha(p2b)}
        ],
        'counters': {
            'bodyBytes': len(p1b) + len(p2b),
            'headerBytes': (len(p1h) + 4) + (len(p2h) + 4),
            'parts': 2, 'fields': 1, 'files': 1
        },
        'bytes': bin1
    })

    # ---- V2: quoted parameters with escapes / separators -------------------
    b2 = 'B2'
    tricky_name = 'na"me;.txt'
    # quoted-string escapes the embedded quote; ';' and '=' need no escaping
    p3h, p3b = part('file', b'AB\tCD', filename=tricky_name.replace('"', '\\"'),
                    content_type='text/plain')
    p4h, p4b = part('a;b=c', 'x="y"')
    bin2 = message(b2, [(p3h, p3b), (p4h, p4b)])
    vectors.append({
        'id': 'v2-quoted-params',
        'boundary': b2,
        'file': 'v2.bin',
        'parts': [
            {'index': 1, 'name': 'file', 'isFile': True,
             'filename': tricky_name, 'contentType': 'text/plain',
             'size': len(p3b), 'sha256': sha(p3b)},
            {'index': 2, 'name': 'a;b=c', 'isFile': False, 'filename': None,
             'contentType': None, 'size': len(p4b), 'sha256': sha(p4b),
             'value': 'x="y"'}
        ],
        'counters': {
            'bodyBytes': len(p3b) + len(p4b),
            'headerBytes': (len(p3h) + 4) + (len(p4h) + 4),
            'parts': 2, 'fields': 1, 'files': 1
        },
        'bytes': bin2
    })

    # ---- V3: boundary-like sequences embedded in the body -------------------
    b3 = 'BOUND'
    decoys = [
        b'--BOUND',                 # no leading CRLF -> not even a candidate
        b'\r\n--BOUNDX',            # wrong char right after boundary
        b'\r\n--BOUND X',           # LWSP then junk instead of CRLF/'--'
        b'\r\n--BOUND--x',          # close marker, but junk after the dashes
        b'\r\n--BOUND-r',           # one dash then a non-dash
        b'\r\n--BOUN' + b'D' + b'q',  # marker split, then a non-delimiter
        b'\r\n--BOUND' + b' ' * 101 + b'\r\n',  # transport padding too long
        b'Z\r\n--BOUND'             # candidate at tail, next byte decides
    ]
    body3 = b'start' + b''.join(decoys) + b'X\r\nreal-end'
    p5h, p5b = part('payload', body3, filename='decoy.txt',
                    content_type='text/plain')
    p6h, p6b = part('note', 'done')
    bin3 = message(b3, [(p5h, p5b), (p6h, p6b)])
    vectors.append({
        'id': 'v3-boundary-decoys-in-body',
        'boundary': b3,
        'file': 'v3.bin',
        'parts': [
            {'index': 1, 'name': 'payload', 'isFile': True,
             'filename': 'decoy.txt', 'contentType': 'text/plain',
             'size': len(p5b), 'sha256': sha(p5b)},
            {'index': 2, 'name': 'note', 'isFile': False, 'filename': None,
             'contentType': None, 'size': len(p6b), 'sha256': sha(p6b),
             'value': 'done'}
        ],
        'counters': {
            'bodyBytes': len(p5b) + len(p6b),
            'headerBytes': (len(p5h) + 4) + (len(p6h) + 4),
            'parts': 2, 'fields': 1, 'files': 1
        },
        'bytes': bin3
    })

    # ---- V4: RFC 5987 filename* wins over filename -------------------------
    b4 = 'xf'
    star = '%E6%96%87%E4%BB%B6.txt'  # 文 件 .txt
    p7h, p7b = part('doc', b'data-4', filename='fallback.txt',
                    filename_star=star, content_type='text/plain')
    bin4 = message(b4, [(p7h, p7b)])
    vectors.append({
        'id': 'v4-filename-star-precedence',
        'boundary': b4,
        'file': 'v4.bin',
        'parts': [
            {'index': 1, 'name': 'doc', 'isFile': True,
             'filename': '文件.txt', 'filenameStar': '文件.txt',
             'filenameStarCharset': 'UTF-8',
             'contentType': 'text/plain',
             'size': len(p7b), 'sha256': sha(p7b)}
        ],
        'counters': {
            'bodyBytes': len(p7b),
            'headerBytes': len(p7h) + 4,
            'parts': 1, 'fields': 0, 'files': 1
        },
        'bytes': bin4
    })

    # ---- V5: empty filename -> the part is a plain field --------------------
    b5 = 'e'
    p8h, p8b = part('emptyfile', 'text-only', filename='')
    bin5 = message(b5, [(p8h, p8b)])
    vectors.append({
        'id': 'v5-empty-filename-is-field',
        'boundary': b5,
        'file': 'v5.bin',
        'parts': [
            {'index': 1, 'name': 'emptyfile', 'isFile': False,
             'filename': None, 'contentType': None,
             'size': len(p8b), 'sha256': sha(p8b), 'value': 'text-only'}
        ],
        'counters': {
            'bodyBytes': len(p8b),
            'headerBytes': len(p8h) + 4,
            'parts': 1, 'fields': 1, 'files': 0
        },
        'bytes': bin5
    })

    # ---- V6: CRLF-shaped / dash-heavy field body, split across close --------
    b6 = 'longboundaryvalue'
    body6 = (b'\r' * 3) + (b'\n' * 3) + (b'-' * 40) + b'\r\n\r\nend'
    p9h, p9b = part('crlf', body6)
    bin6 = message(b6, [(p9h, p9b)])
    vectors.append({
        'id': 'v6-crlf-dash-heavy-body',
        'boundary': b6,
        'file': 'v6.bin',
        'parts': [
            {'index': 1, 'name': 'crlf', 'isFile': False, 'filename': None,
             'contentType': None, 'size': len(p9b), 'sha256': sha(p9b)}
        ],
        'counters': {
            'bodyBytes': len(p9b),
            'headerBytes': len(p9h) + 4,
            'parts': 1, 'fields': 1, 'files': 0
        },
        'bytes': bin6
    })

    # ---- N1: no terminating boundary ---------------------------------------
    nb = 'NB'
    nh, nbody = part('f', 'abc')
    bin_n1 = message(nb, [(nh, nbody)], terminate=False)
    negatives.append({
        'id': 'n1-missing-terminating-boundary',
        'boundary': nb, 'file': 'n1.bin',
        'errorCode': 'MISSING_TERMINATING_BOUNDARY',
        'errorClass': 'INPUT_ERROR',
        'bytes': bin_n1
    })

    # ---- N2: path traversal filename ---------------------------------------
    nh2, nbody2 = part('f', b'x', filename='../../etc/passwd',
                       content_type='text/plain')
    bin_n2 = message(nb, [(nh2, nbody2)])
    negatives.append({
        'id': 'n2-path-traversal-filename',
        'boundary': nb, 'file': 'n2.bin',
        'errorCode': 'PATH_TRAVERSAL_FILENAME',
        'errorClass': 'INPUT_ERROR',
        'bytes': bin_n2
    })

    # ---- N3: duplicate Content-Disposition ---------------------------------
    raw = [
        'Content-Disposition: form-data; name="a"',
        'Content-Disposition: form-data; name="b"'
    ]
    nh3, nbody3 = part(None, b'x', raw_headers=raw)
    bin_n3 = message(nb, [(nh3, nbody3)])
    negatives.append({
        'id': 'n3-duplicate-content-disposition',
        'boundary': nb, 'file': 'n3.bin',
        'errorCode': 'DUPLICATE_CONTENT_DISPOSITION',
        'errorClass': 'INPUT_ERROR',
        'bytes': bin_n3
    })

    # ---- N4: unterminated quoted filename ----------------------------------
    raw4 = ['Content-Disposition: form-data; name="f"; filename="abc.txt']
    nh4, nbody4 = part(None, b'x', raw_headers=raw4)
    bin_n4 = message(nb, [(nh4, nbody4)])
    negatives.append({
        'id': 'n4-unterminated-quoted-filename',
        'boundary': nb, 'file': 'n4.bin',
        'errorCode': 'MALFORMED_HEADER_SYNTAX',
        'errorClass': 'INPUT_ERROR',
        'bytes': bin_n4
    })

    # ---- N5: entity does not start with the opening delimiter ---------------
    bin_n5 = b'prologue junk' + message(nb, [part('f', 'x')])
    negatives.append({
        'id': 'n5-prologue-not-empty',
        'boundary': nb, 'file': 'n5.bin',
        'errorCode': 'PROLOGUE_NOT_EMPTY',
        'errorClass': 'INPUT_ERROR',
        'bytes': bin_n5
    })

    return vectors, negatives


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else 'fixtures/golden'
    os.makedirs(out, exist_ok=True)
    vectors, negatives = build()
    manifest = {'positive': [], 'negative': []}
    for v in vectors + negatives:
        data = v.pop('bytes')
        with open(os.path.join(out, v['file']), 'wb') as fh:
            fh.write(data)
        v['wireLength'] = len(data)
    for v in vectors:
        manifest['positive'].append(v)
    for v in negatives:
        manifest['negative'].append(v)
    with open(os.path.join(out, 'golden-manifest.json'), 'w', encoding='utf-8') as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write('\n')
    print(f'wrote {len(vectors)} positive and {len(negatives)} negative vectors to {out}')


if __name__ == '__main__':
    main()
