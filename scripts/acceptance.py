#!/usr/bin/env python3
"""End-to-end acceptance driver for smtprecv.

Speaks raw SMTP over a loopback TCP socket so that framing, dot
transparency, out-of-order rejection, oversize handling and mid-DATA
interruption can be exercised deterministically. Then queries the HTTP
API. Run the server first:

    ./bin/smtprecv -smtp-addr 127.0.0.1:2525 -http-addr 127.0.0.1:8080 \
        -data-dir /tmp/smtprecv-demo
    python3 scripts/acceptance.py
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

SMTP_HOST = os.environ.get("SMTP_HOST", "127.0.0.1")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "2525"))
HTTP_PORT = int(os.environ.get("HTTP_PORT", "8080"))
SMTP = (SMTP_HOST, SMTP_PORT)
HTTP = f"http://{SMTP_HOST}:{HTTP_PORT}"

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name} {detail}")


def smtp_connect():
    s = socket.create_connection(SMTP, timeout=5)
    f = s.makefile("rb")
    greeting = f.readline().decode().strip()
    check("server greets 220", greeting.startswith("220 "), greeting)
    return s, f


def send_line(f, s, raw):
    s.sendall(raw)
    out = []
    while True:
        line = f.readline().decode().strip()
        if not line:
            break
        out.append(line)
        if len(line) >= 4 and line[3] == " ":
            break
    return "\n".join(out)


def main():
    print("== scenario 1: full message, multiple recipients, single dot in body ==")
    s, f = smtp_connect()
    r = send_line(f, s, b"EHLO acceptance\r\n")
    check("EHLO 250", r.startswith("250-"), r)
    r = send_line(f, s, b"MAIL FROM:<alice@example.com>\r\n")
    check("MAIL 250", r.startswith("250 "), r)
    for rcpt in ("bob@example.com", "carol@example.com", "dave@example.com"):
        r = send_line(f, s, f"RCPT TO:<{rcpt}>\r\n".encode())
        check(f"RCPT {rcpt} 250", r.startswith("250 "), r)
    r = send_line(f, s, b"DATA\r\n")
    check("DATA 354", r.startswith("354 "), r)
    # Body deliberately contains:
    #  - a line that is a literal single dot  -> sent dot-stuffed as ".."
    #  - a line starting with "...."          -> one leading dot removed
    #  - ordinary lines
    body = (
        b"Subject: acceptance\r\n"
        b"\r\n"
        b"line before the dot\r\n"
        b"..\r\n"                  # dot-stuffed single dot line
        b"....quad dots\r\n"       # becomes "...quad dots"
        b"line after\r\n"
        b".\r\n"
    )
    r = send_line(f, s, body)
    check("message accepted 250", r.startswith("250 "), r)
    r = send_line(f, s, b"QUIT\r\n")
    check("QUIT 221", r.startswith("221 "), r)
    s.close()

    print("== scenario 2: out-of-order commands rejected 503 ==")
    s, f = smtp_connect()
    r = send_line(f, s, b"MAIL FROM:<a@x>\r\n")
    check("MAIL before EHLO -> 503", r.startswith("503 "), r)
    r = send_line(f, s, b"RCPT TO:<b@x>\r\n")
    check("RCPT before EHLO -> 503", r.startswith("503 "), r)
    r = send_line(f, s, b"DATA\r\n")
    check("DATA before EHLO -> 503", r.startswith("503 "), r)
    send_line(f, s, b"EHLO a\r\n")
    r = send_line(f, s, b"RCPT TO:<b@x>\r\n")
    check("RCPT before MAIL -> 503", r.startswith("503 "), r)
    r = send_line(f, s, b"DATA\r\n")
    check("DATA before RCPT -> 503", r.startswith("503 "), r)
    # bare LF command rejected
    r = send_line(f, s, b"RSET\n")
    check("bare-LF command -> 501", r.startswith("501 "), r)
    send_line(f, s, b"QUIT\r\n")
    s.close()

    print("== scenario 3: RSET clears the envelope ==")
    s, f = smtp_connect()
    send_line(f, s, b"EHLO a\r\n")
    send_line(f, s, b"MAIL FROM:<a@x>\r\n")
    send_line(f, s, b"RCPT TO:<b@x>\r\n")
    r = send_line(f, s, b"RSET\r\n")
    check("RSET 250", r.startswith("250 "), r)
    r = send_line(f, s, b"DATA\r\n")
    check("DATA after RSET -> 503", r.startswith("503 "), r)
    send_line(f, s, b"QUIT\r\n")
    s.close()

    print("== scenario 4: oversized message rejected, NOT stored ==")
    s, f = smtp_connect()
    send_line(f, s, b"EHLO a\r\n")
    # Server is started with -max-size 1024 for this demo.
    send_line(f, s, b"MAIL FROM:<big@x>\r\n")
    send_line(f, s, b"RCPT TO:<b@x>\r\n")
    send_line(f, s, b"DATA\r\n")
    s.sendall(b"x" * 2048 + b"\r\n")
    r = send_line(f, s, b".\r\n")
    check("oversize -> 552", r.startswith("552 "), r)
    send_line(f, s, b"QUIT\r\n")
    s.close()

    print("== scenario 5: DATA interrupted (TCP drop), NOT stored ==")
    s, f = smtp_connect()
    send_line(f, s, b"EHLO a\r\n")
    send_line(f, s, b"MAIL FROM:<partial@x>\r\n")
    send_line(f, s, b"RCPT TO:<b@x>\r\n")
    send_line(f, s, b"DATA\r\n")
    s.sendall(b"this body never sees the terminating dot line")
    s.close()  # hard drop mid-DATA
    time.sleep(0.3)
    check("interrupted DATA stored nothing", True)  # verified via API below

    print("== scenario 6: HELO + tiny message still lands atomically ==")
    s, f = smtp_connect()
    send_line(f, s, b"HELO legacy\r\n")
    send_line(f, s, b"MAIL FROM:<helo@x>\r\n")
    send_line(f, s, b"RCPT TO:<b@x>\r\n")
    send_line(f, s, b"DATA\r\n")
    r = send_line(f, s, b"tiny body\r\n.\r\n")
    check("HELO message accepted", r.startswith("250 "), r)
    send_line(f, s, b"QUIT\r\n")
    s.close()

    print("== HTTP API verification ==")
    with urllib.request.urlopen(HTTP + "/healthz", timeout=5) as resp:
        health = json.load(resp)
    check("/healthz ok", health.get("status") == "ok", str(health))

    with urllib.request.urlopen(HTTP + "/messages", timeout=5) as resp:
        listing = json.load(resp)
    messages = listing["messages"]
    print(f"  stored messages: {len(messages)} (expect 2: acceptance + helo)")
    check("exactly 2 completed messages stored", len(messages) == 2,
          f"got {len(messages)}: {[m['from'] for m in messages]}")
    check("no oversize sender present",
          all(m["from"] != "big@x" for m in messages))
    check("no interrupted sender present",
          all(m["from"] != "partial@x" for m in messages))
    check("listings omit bodies", all(m["data"] == "" for m in messages))

    # Newest first: helo message is list[0], acceptance list[1].
    helo_msg = messages[0]
    check("helo recipient recorded", helo_msg["to"] == ["b@x"], str(helo_msg["to"]))

    acc = messages[1]
    check("acceptance sender", acc["from"] == "alice@example.com", acc["from"])
    check("acceptance has 3 recipients", len(acc["to"]) == 3, str(acc["to"]))

    with urllib.request.urlopen(f"{HTTP}/messages/{acc['id']}", timeout=5) as resp:
        full = json.load(resp)
    data = full["data"]
    check("dot-transparency: literal single dot preserved",
          "line before the dot\r\n.\r\n" in data, repr(data))
    check("dot-transparency: leading dots unstuffed",
          "...quad dots\r\n" in data, repr(data))
    check("body keeps CRLF framing", "\r\n" in data and "\n\n" not in
          data.replace("\r\n", ""))
    check("body ends with CRLF before terminator", data.endswith("line after\r\n"),
          repr(data[-40:]))

    with urllib.request.urlopen(f"{HTTP}/messages/{acc['id']}/raw", timeout=5) as resp:
        raw = resp.read().decode()
        check("/raw serves message/rfc822",
              resp.headers["Content-Type"] == "message/rfc822")
        check("/raw byte-identical to stored data", raw == data)

    # 404 path
    try:
        urllib.request.urlopen(HTTP + "/messages/does-not-exist", timeout=5)
        check("missing id -> 404", False)
    except urllib.error.HTTPError as e:
        check("missing id -> 404", e.code == 404, str(e.code))

    print()
    print(f"RESULT: {passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
