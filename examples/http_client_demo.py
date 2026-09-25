"""Request samples using only the Python standard library (no third-party deps).

Run a server first:
    python3 -m enrange keygen --data-dir ./data
    python3 -m enrange serve  --data-dir ./data --host 127.0.0.1 --port 8099
then:
    python3 examples/http_client_demo.py
"""

import http.client
import json

HOST, PORT = "127.0.0.1", 8099


def call(method, path, body=b"", headers=None):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
    conn.request(method, path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    return resp.status, dict(resp.getheaders()), data


def main() -> None:
    # Create object with a server-generated id (block size 16 for demo).
    payload = b"Z" * 50
    status, _, body = call(
        "POST", "/objects?block_size=16", payload,
        {"Content-Length": str(len(payload))},
    )
    assert status == 201, body
    meta = json.loads(body)
    oid = meta["id"]
    print("created", meta)

    # Full object.
    status, headers, body = call("GET", f"/objects/{oid}")
    print("full  ", status, len(body), headers.get("Content-Range"))

    # First byte.
    status, headers, body = call(
        "GET", f"/objects/{oid}", headers={"Range": "bytes=0-0"}
    )
    print("first ", status, body, headers.get("Content-Range"))

    # Last byte of the short final block.
    status, headers, body = call(
        "GET", f"/objects/{oid}", headers={"Range": "bytes=49-49"}
    )
    print("last  ", status, body, headers.get("Content-Range"))

    # Cross-block slice using the half-open query API.
    status, headers, body = call("GET", f"/objects/{oid}?start=14&end=20")
    print("slice ", status, body, headers.get("Content-Range"))

    # Authenticated metadata.
    status, _, body = call("GET", f"/objects/{oid}/meta")
    print("meta  ", status, json.loads(body))

    # Empty object.
    eid = "ee" * 16
    call("PUT", f"/objects/{eid}", b"", {"Content-Length": "0"})
    status, headers, body = call("GET", f"/objects/{eid}")
    print("empty ", status, len(body), "len-header=", headers.get("Content-Length"))


if __name__ == "__main__":
    main()
