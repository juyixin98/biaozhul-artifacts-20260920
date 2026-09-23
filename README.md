# loopmail — loopback-only SMTP test receiver

A minimal, **receive-only** SMTP server for testing, written in pure Go with
the standard library (`net`, `net/http`) — **no third-party dependencies**.
It accepts mail over SMTP, stores every fully-completed message atomically on
the local filesystem, and exposes them over a small HTTP API. **It never sends
or relays mail anywhere.**

It is safe to point test applications at because it can only ever listen on
and accept connections from the loopback interface.

## Features

- SMTP subset: `EHLO`/`HELO`, `MAIL FROM`, `RCPT TO`, `DATA`, `RSET`,
  `NOOP`, `VRFY`, `QUIT`.
- Commands out of order are rejected with `503` (e.g. `MAIL` before `EHLO`,
  `RCPT` before `MAIL`, `DATA` before `RCPT`).
- Strict CRLF framing and RFC 5321 **dot-transparency** (a body line that is a
  single `.` round-trips correctly).
- **Atomic storage**: a message is written to a temp file, synced, and renamed
  into place. Only a properly terminated DATA (`<CRLF>.<CRLF>` followed by
  `250`) is ever visible. A disconnect, timeout, or oversize DATA stores
  nothing and leaves no partial file.
- Size cap (advertised via `SIZE`), oversize DATA rejected with `552`.
- Empty `MAIL FROM:<>` (bounce style) supported; multiple recipients
  preserved per message.
- HTTP API to list / fetch (raw RFC822) / delete messages.
- Loopback-only enforcement, twice:
  1. the listen host must be a literal loopback IP (`127.0.0.1` / `::1`) —
     hostnames like `localhost` and non-loopback IPs are refused at startup;
  2. any accepted peer whose remote address is not loopback is dropped.

## Requirements

- Go **1.23+** (toolchain used in development: go1.23.4 linux/amd64).
- No external services, no CGO, no network access needed to build or test.

## Build and run

```sh
# build
go build -o loopmail .

# run (defaults shown)
./loopmail \
  -smtp-addr 127.0.0.1:2525 \
  -http-addr 127.0.0.1:8080 \
  -data-dir  ./data \
  -max-size-kb 1024 \
  -idle-timeout-sec 120
```

Or without a build step:

```sh
go run . -smtp-addr 127.0.0.1:2525 -http-addr 127.0.0.1:8080
```

Flags:

| flag                 | default            | meaning                                        |
|----------------------|--------------------|------------------------------------------------|
| `-smtp-addr`         | `127.0.0.1:2525`   | SMTP listen address; host must be loopback IP  |
| `-http-addr`         | `127.0.0.1:8080`   | HTTP API listen address; host must be loopback |
| `-data-dir`          | `./data`           | spool directory (`msgs/` and `tmp/` live here) |
| `-max-size-kb`       | `1024`             | max accepted DATA payload in KiB               |
| `-idle-timeout-sec`  | `120`              | per-line read timeout                          |

Press Ctrl-C for graceful shutdown (listener and active connections close;
in-flight DATA is discarded, never half-stored).

## HTTP API

All endpoints bind to loopback only.

| Method | path                  | description                                   |
|--------|-----------------------|-----------------------------------------------|
| GET    | `/healthz`            | liveness probe                                |
| GET    | `/messages`           | JSON list of stored messages (no raw bodies)  |
| GET    | `/messages/{id}`      | the raw RFC822 message (`message/rfc822`)     |
| DELETE | `/messages/{id}`      | remove a message (`204`)                      |

### Examples

```sh
# health
curl -s http://127.0.0.1:8080/healthz

# list
curl -s http://127.0.0.1:8080/messages

# fetch one raw message
curl -s http://127.0.0.1:8080/messages/<id>

# delete
curl -s -X DELETE http://127.0.0.1:8080/messages/<id>
```

List response:

```json
{
  "count": 1,
  "messages": [
    {
      "id": "20260923T163415.632136328-1fb83300071bf3bf0d1df7e668267a04",
      "from": "alice@demo.test",
      "to": ["bob@demo.test", "carol@demo.test"],
      "subject": "Acceptance 1",
      "size": 65,
      "received_at": "2026-09-24T00:34:15.632136328+08:00"
    }
  ]
}
```

## Sending example messages

### Go client (standard library)

```sh
go run ./examples/send -addr 127.0.0.1:2525
```

### Raw SMTP over netcat

Use a CRLF-aware netcat (OpenBSD nc: `-C`). Note the dot-stuffed body lines
(`..` denotes a content line that is a single dot):

```sh
printf 'EHLO demo.test\r\n\
MAIL FROM:<alice@demo.test>\r\n\
RCPT TO:<bob@demo.test>\r\n\
RCPT TO:<carol@demo.test>\r\n\
DATA\r\n\
Subject: Acceptance 1\r\n\
\r\n\
line before\r\n\
..\r\n\
..two dots\r\n\
normal line\r\n\
.\r\n\
QUIT\r\n' | nc -C -q 2 127.0.0.1 2525
```

Expected transcript:

```
220 loopmail.local ESMTP loopmail (test receiver; no outbound delivery)
250-loopmail.local at your service
250-8BITMIME
250 SIZE 1048576
250 2.1.0 OK
250 2.1.5 OK
250 2.1.5 OK
354 Start mail input; end with <CRLF>.<CRLF>
250 2.0.0 OK stored as 20260923T163415.632136328-...
221 2.0.0 Bye
```

## On-disk format

Each completed message is `<data-dir>/msgs/<id>.eml`: a single JSON envelope
line followed immediately by the raw RFC822 payload, e.g.

```
{"id":"...","from":"alice@demo.test","to":["bob@demo.test","carol@demo.test"],"received_at":"...","size":65}
Subject: Acceptance 1

line before
.
.two dots
normal line
```

This makes the spool self-describing and lets the in-memory index be rebuilt
on restart. Files in `<data-dir>/tmp/` are staging files; they are renamed
atomically into `msgs/` on success and swept on startup if a prior process
died mid-DATA.

## Project layout

```
.
├── go.mod                  # module loopmail; no require blocks (stdlib only)
├── main.go                 # entry point: wires store + SMTP + HTTP
├── store/                  # atomic filesystem message store (+ tests)
├── smtpd/                  # SMTP server / state machine (+ tests)
├── httpapi/                # net/http JSON API (+ tests)
├── main_e2e_test.go        # end-to-end: net/smtp client -> store -> HTTP
└── examples/send/main.go   # example SMTP client
```

## Tests

```sh
go test ./...            # all packages
go test -race ./...      # with the race detector
go test -v ./smtpd/      # verbose SMTP protocol cases
go test -cover ./...     # coverage (core packages ~77-82%)
```

A dependency-free Python script drives all four acceptance scenarios over real
sockets and asserts the results through the HTTP API. Terminal 1 runs the
server with a 4 KiB-limit second instance; terminal 2 runs the checks:

```sh
# terminal 1
./loopmail -smtp-addr 127.0.0.1:2525 -http-addr 127.0.0.1:8080 -data-dir /tmp/lm
./loopmail -smtp-addr 127.0.0.1:2526 -http-addr 127.0.0.1:8081 \
  -data-dir /tmp/lm-small -max-size-kb 4

# terminal 2
python3 examples/acceptance/acceptance.py
```

It verifies: a complete multi-recipient mail whose body contains a single-dot
line (dot transparency), `503` for out-of-order commands, `552` for an
oversize message, that an abruptly interrupted DATA stores nothing, and that
exactly one complete message is visible over HTTP with no orphan temp files.

Covered behavior:

- greeting / EHLO banner; out-of-order `503` rejections; unknown command.
- multiple recipients; dot-transparency incl. a body line that is just `.`.
- CRLF framing; empty `MAIL FROM:<>`.
- oversize DATA → `552` and **nothing stored**.
- abrupt connection drop mid-DATA → **nothing stored**.
- `RSET` clears the transaction but keeps greeting; repeated EHLO resets it.
- sink save failure → `451`, session stays usable.
- refusal to bind non-loopback addresses (`0.0.0.0`, `8.8.8.8`, `localhost`).
- atomic store: no stray temp files; index rebuilt after restart; concurrent
  saves produce unique IDs.
- HTTP list / fetch raw / 404 / delete.
- end-to-end with the real `net/smtp` client through store to HTTP.

## Scope and non-goals (intentionally not implemented)

- No outbound delivery, relay, forwarding, or MX lookups.
- No authentication/TLS/STARTTLS (loopback test tool only).
- No address validation beyond envelope syntax; VRFY returns `252`.
- Subject preview in the list API does not unfold headers or decode RFC 2047
  encoded-words; the raw message is always available intact via
  `GET /messages/{id}`.
- Bare-LF line endings are tolerated when reading commands/body (many test
  clients send them); stored content is always normalized to CRLF.
