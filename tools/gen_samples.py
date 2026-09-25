#!/usr/bin/env python3
"""Generate the request-sample corpus for the HTTP framing project.

Each sample is one raw file under samples/ with a manifest (samples.json)
recording whether it is expected to be ACCEPTED or REJECTED (and, on
reject, the expected ErrorKind code).

All bytes are written exactly as a TCP peer would send them (CRLF and all).

Usage:
    python3 tools/gen_samples.py
"""

from __future__ import annotations

import json
import os
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "samples"


def CRLF(s: str) -> bytes:
    """Turn a text spec with \\n line endings into exact CRLF bytes."""
    return s.replace("\n", "\r\n").encode()


# (filename, bytes, accepted: bool, expected error code or None, notes)
SAMPLES: list[tuple[str, bytes, bool, str | None, str]] = []


def add(name: str, data: bytes, accepted: bool, error: str | None, notes: str):
    SAMPLES.append((name, data, accepted, error, notes))


# ---------------------------------------------------------------- valid --
add(
    "01-get-minimal.http",
    CRLF("GET / HTTP/1.1\n\n"),
    True,
    None,
    "GET with no headers and no body",
)
add(
    "02-get-host.http",
    CRLF("GET /index.html HTTP/1.1\nHost: example.com\nUser-Agent: demo\n\n"),
    True,
    None,
    "ordinary GET with two headers",
)
add(
    "03-post-fixed.http",
    CRLF("POST /upload HTTP/1.1\nHost: example.com\nContent-Length: 11\n\n")
    + b"hello world",
    True,
    None,
    "fixed-length body (Content-Length: 11)",
)
add(
    "04-post-fixed-zero.http",
    CRLF("POST /empty HTTP/1.1\nContent-Length: 0\n\n"),
    True,
    None,
    "fixed-length zero body",
)
add(
    "05-chunked-simple.http",
    CRLF(
        "POST /c HTTP/1.1\n"
        "Host: example.com\n"
        "Transfer-Encoding: chunked\n"
        "\n"
        "5\n"
        "hello\n"
        "6\n"
        " world\n"
        "0\n"
        "\n"
    ),
    True,
    None,
    "two chunks, no trailers",
)
add(
    "06-chunked-trailer.http",
    CRLF(
        "POST /c HTTP/1.1\n"
        "Transfer-Encoding: chunked\n"
        "\n"
        "5\n"
        "hello\n"
        "0\n"
        "X-Checksum: deadbeef\n"
        "X-Signed: yes\n"
        "\n"
    ),
    True,
    None,
    "chunked body with two trailer fields",
)
add(
    "07-chunked-ext.http",
    CRLF(
        "POST /c HTTP/1.1\n"
        "Transfer-Encoding: chunked\n"
        "\n"
        '5; name="part 1"\n'
        "hello\n"
        "0; fin\n"
        "\n"
    ),
    True,
    None,
    "chunk extensions, including quoted-string value",
)
add(
    "08-chunked-multi.http",
    # "Wikipedia in\r\n\r\nchunks." assembled in chunks (RFC 7230 example).
    CRLF("POST /c HTTP/1.1\nTransfer-Encoding: chunked\n\n")
    + b"4\r\nWiki\r\n"
    + b"5\r\npedia\r\n"
    + b"E\r\n in\r\n\r\nchunks.\r\n"
    + b"0\r\n\r\n",
    True,
    None,
    "multiple chunks, body contains literal CRLF",
)
add(
    "09-pipeline-two.http",
    CRLF("GET /a HTTP/1.1\nHost: e\n\nGET /b HTTP/1.1\nHost: e\n\n"),
    True,
    None,
    "two pipelined GETs in one TCP stream",
)
add(
    "10-pipeline-get-post.http",
    CRLF("GET /a HTTP/1.1\n\nPOST /b HTTP/1.1\nContent-Length: 3\n\n") + b"abc",
    True,
    None,
    "pipelined GET then fixed-length POST",
)
add(
    "11-cl-agreed-list.http",
    CRLF("POST / HTTP/1.1\nContent-Length: 5, 5\n\n") + b"hello",
    True,
    None,
    "RFC 9112 allowed form: repeated identical values in one CL field",
)
add(
    "12-headers-empty-value.http",
    CRLF("GET / HTTP/1.1\nX-Empty:\nX-Space:   \n\n"),
    True,
    None,
    "empty and OWS-only header values",
)
add(
    "13-connection-close.http",
    CRLF("GET / HTTP/1.1\nHost: e\nConnection: close\n\n"),
    True,
    None,
    "explicit keep-alive shutdown",
)
add(
    "14-chunked-uppercase.http",
    CRLF("POST /c HTTP/1.1\nTRANSFER-ENCODING: CHUNKED\n\n4\nWIKI\n0\n\n"),
    True,
    None,
    "header names and the chunked coding are case insensitive",
)

# -------------------------------------------------------------- invalid --
add(
    "20-bare-lf.http",
    b"GET / HTTP/1.1\nHost: example.com\n\n",
    False,
    "bare_line_feed",
    "request line ends with bare LF (no CR)",
)
add(
    "21-bare-cr.http",
    b"GET / HTTP/1.1\rHost: x\r\n\r\n",
    False,
    "bare_carriage_return",
    "bare CR in the request line",
)
add(
    "22-obs-fold.http",
    CRLF("GET / HTTP/1.1\nX: a\n b\n\n"),
    False,
    "obs_fold",
    "obsolete line folding (continuation starts with SP)",
)
add(
    "23-space-before-colon.http",
    CRLF("GET / HTTP/1.1\nX : y\n\n"),
    False,
    "invalid_header_name",
    "space between header name and colon (RFC 9112 smuggling case)",
)
add(
    "24-cl-te.http",
    CRLF(
        "POST / HTTP/1.1\n"
        "Content-Length: 6\n"
        "Transfer-Encoding: chunked\n"
        "\n"
        "0\n"
        "\n"
    ),
    False,
    "te_and_cl",
    "TE and CL present together (CL.TE ambiguity)",
)
add(
    "25-te-cl.http",
    CRLF(
        "POST / HTTP/1.1\n"
        "Transfer-Encoding: chunked\n"
        "Content-Length: 6\n"
        "\n"
        "0\n"
        "\n"
    ),
    False,
    "te_and_cl",
    "TE and CL present together, reverse order (TE.CL ambiguity)",
)
add(
    "26-duplicate-cl.http",
    CRLF("POST / HTTP/1.1\nContent-Length: 5\nContent-Length: 5\n\n") + b"hello",
    False,
    "duplicate_content_length",
    "two CL header fields, even with equal values",
)
add(
    "27-conflicting-cl-list.http",
    CRLF("POST / HTTP/1.1\nContent-Length: 5, 6\n\n") + b"hello",
    False,
    "conflicting_content_length",
    "comma list of CL values that disagree",
)
add(
    "28-te-not-chunked.http",
    CRLF("POST / HTTP/1.1\nTransfer-Encoding: chunked, identity\n\n0\n\n"),
    False,
    "invalid_transfer_encoding",
    "TE lists more than the single chunked coding",
)
add(
    "29-cl-non-numeric.http",
    CRLF("POST / HTTP/1.1\nContent-Length: 0x10\n\n"),
    False,
    "invalid_content_length",
    "CL value is not a decimal integer",
)
add(
    "30-duplicate-te.http",
    CRLF(
        "POST / HTTP/1.1\n"
        "Transfer-Encoding: chunked\n"
        "Transfer-Encoding: chunked\n"
        "\n0\n\n"
    ),
    False,
    "invalid_transfer_encoding",
    "two TE header fields",
)
add(
    "31-malformed-request-line.http",
    CRLF("GET  / HTTP/1.1\n\n"),
    False,
    "malformed_request_line",
    "double SP in request line",
)
add(
    "32-http10.http",
    CRLF("GET / HTTP/1.0\n\n"),
    False,
    "unsupported_version",
    "only HTTP/1.1 is supported",
)
add(
    "33-bad-chunk-size.http",
    CRLF("POST /c HTTP/1.1\nTransfer-Encoding: chunked\n\nxx\n"),
    False,
    "malformed_chunk_size",
    "chunk size is not hexadecimal",
)
add(
    "34-trailer-cl.http",
    CRLF("POST /c HTTP/1.1\nTransfer-Encoding: chunked\n\n0\nContent-Length: 0\n\n"),
    False,
    "forbidden_trailer_field",
    "Content-Length smuggled into trailers",
)
add(
    "35-bad-chunk-ext.http",
    CRLF("POST /c HTTP/1.1\nTransfer-Encoding: chunked\n\n5;\x01\n"),
    False,
    "invalid_chunk_extension",
    "control byte in chunk extension",
)
add(
    "36-header-no-colon.http",
    CRLF("GET / HTTP/1.1\nNotAHeader\n\n"),
    False,
    "malformed_header",
    "header line without a colon",
)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name, data, accepted, error, notes in SAMPLES:
        path = OUT / name
        path.write_bytes(data)
        manifest.append(
            {
                "file": name,
                "bytes": len(data),
                "expected": "accepted" if accepted else "rejected",
                "error": error,
                "notes": notes,
            }
        )
    (OUT / "samples.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {len(SAMPLES)} samples to {OUT}")


if __name__ == "__main__":
    main()
