package smtpd

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"loopmail/store"
)

// fakeSink records saves and can inject failures.
type fakeSink struct {
	mu   sync.Mutex
	msgs []savedMsg
	fail bool
}

type savedMsg struct {
	from string
	to   []string
	raw  []byte
}

func (f *fakeSink) Save(from string, to []string, raw []byte, _ time.Time) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.fail {
		return "", errors.New("disk full")
	}
	id := fmt.Sprintf("msg-%d", len(f.msgs)+1)
	f.msgs = append(f.msgs, savedMsg{from: from, to: append([]string(nil), to...), raw: append([]byte(nil), raw...)})
	return id, nil
}

func (f *fakeSink) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.msgs)
}

// testServer starts a server on an ephemeral loopback port with the given sink.
func testServer(t *testing.T, sink MailSink, maxBytes int) *Server {
	t.Helper()
	srv, err := New(Config{
		Addr:            "127.0.0.1:0",
		Hostname:        "test.local",
		MaxMessageBytes: maxBytes,
		IdleTimeout:     5 * time.Second,
		Sink:            sink,
	})
	if err != nil {
		t.Fatalf("New: %v", err)
	}
	if err := srv.Listen(); err != nil {
		t.Fatalf("Listen: %v", err)
	}
	go func() { _ = srv.Serve() }()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	})
	return srv
}

func dialServer(t *testing.T, addr net.Addr) (net.Conn, *bufio.Reader) {
	t.Helper()
	c, err := net.Dial("tcp", addr.String())
	if err != nil {
		t.Fatalf("dial: %v", err)
	}
	t.Cleanup(func() { c.Close() })
	br := bufio.NewReader(c)
	return c, br
}

func expectCode(t *testing.T, br *bufio.Reader, want string) string {
	t.Helper()
	line, err := br.ReadString('\n')
	if err != nil {
		t.Fatalf("expected reply %q, read error: %v", want, err)
	}
	line = strings.TrimRight(line, "\r\n")
	if !strings.HasPrefix(line, want) {
		t.Fatalf("expected reply starting %q, got %q", want, line)
	}
	return line
}

// readMultiline reads until a final line whose 4th character is a space.
func readMultiline(t *testing.T, br *bufio.Reader, code string) []string {
	t.Helper()
	var out []string
	for {
		line, err := br.ReadString('\n')
		if err != nil {
			t.Fatalf("multiline read: %v", err)
		}
		line = strings.TrimRight(line, "\r\n")
		out = append(out, line)
		if strings.HasPrefix(line, code+" ") {
			return out
		}
		if !strings.HasPrefix(line, code+"-") {
			t.Fatalf("malformed multiline reply: %q", line)
		}
	}
}

func send(t *testing.T, c net.Conn, format string, args ...any) {
	t.Helper()
	if _, err := fmt.Fprintf(c, format+"\r\n", args...); err != nil {
		t.Fatalf("send %q: %v", format, err)
	}
}

// deliver performs a normal full transaction and returns the final reply.
func deliver(t *testing.T, c net.Conn, br *bufio.Reader, from string, rcpts []string, dataLines []string) string {
	t.Helper()
	send(t, c, "EHLO tester")
	readMultiline(t, br, "250")
	send(t, c, "MAIL FROM:<%s>", from)
	expectCode(t, br, "250")
	for _, r := range rcpts {
		send(t, c, "RCPT TO:<%s>", r)
		expectCode(t, br, "250")
	}
	send(t, c, "DATA")
	expectCode(t, br, "354")
	for _, l := range dataLines {
		// RFC 5321 client dot-stuffing: a content line starting with a dot
		// is sent with one extra leading dot.
		if strings.HasPrefix(l, ".") {
			l = "." + l
		}
		send(t, c, "%s", l)
	}
	send(t, c, ".")
	return expectCode(t, br, "250")
}

func TestGreetingAndEhlo(t *testing.T) {
	srv := testServer(t, &fakeSink{}, 0)
	c, br := dialServer(t, srv.Addr())

	line := expectCode(t, br, "220")
	if !strings.Contains(line, "test.local") {
		t.Fatalf("greeting missing hostname: %q", line)
	}
	send(t, c, "EHLO client.example")
	lines := readMultiline(t, br, "250")
	joined := strings.Join(lines, "\n")
	if !strings.Contains(joined, "8BITMIME") {
		t.Errorf("EHLO banner missing 8BITMIME: %q", joined)
	}
	if !strings.Contains(joined, "SIZE ") {
		t.Errorf("EHLO banner missing SIZE: %q", joined)
	}
}

func TestOutOfOrderRejections(t *testing.T) {
	srv := testServer(t, &fakeSink{}, 0)

	t.Run("MAIL before EHLO", func(t *testing.T) {
		c, br := dialServer(t, srv.Addr())
		expectCode(t, br, "220")
		send(t, c, "MAIL FROM:<a@example>")
		expectCode(t, br, "503")
	})

	t.Run("RCPT before MAIL", func(t *testing.T) {
		c, br := dialServer(t, srv.Addr())
		expectCode(t, br, "220")
		send(t, c, "EHLO x")
		readMultiline(t, br, "250")
		send(t, c, "RCPT TO:<b@example>")
		expectCode(t, br, "503")
	})

	t.Run("DATA before RCPT", func(t *testing.T) {
		c, br := dialServer(t, srv.Addr())
		expectCode(t, br, "220")
		send(t, c, "EHLO x")
		readMultiline(t, br, "250")
		send(t, c, "MAIL FROM:<a@example>")
		expectCode(t, br, "250")
		send(t, c, "DATA")
		expectCode(t, br, "503")
	})

	t.Run("RCPT inside DATA is content, not a command", func(t *testing.T) {
		c, br := dialServer(t, srv.Addr())
		expectCode(t, br, "220")
		send(t, c, "EHLO x")
		readMultiline(t, br, "250")
		send(t, c, "MAIL FROM:<a@example>")
		expectCode(t, br, "250")
		send(t, c, "RCPT TO:<b@example>")
		expectCode(t, br, "250")
		send(t, c, "DATA")
		expectCode(t, br, "354")
		// A line that looks like a command while DATA is open is body text.
		send(t, c, "RCPT TO:<evil@example>")
		send(t, c, ".")
		expectCode(t, br, "250")
	})

	t.Run("unknown command and malformed syntax", func(t *testing.T) {
		c, br := dialServer(t, srv.Addr())
		expectCode(t, br, "220")
		send(t, c, "EHLO x")
		readMultiline(t, br, "250")
		send(t, c, "FROBNICATE")
		expectCode(t, br, "502")
		// MAIL without the FROM: keyword is a syntax error.
		send(t, c, "MAIL a@example")
		expectCode(t, br, "501")
		send(t, c, "MAIL FROM:<a@example>")
		expectCode(t, br, "250")
		// RCPT without the TO: keyword is a syntax error now that MAIL is open.
		send(t, c, "RCPT a@example")
		expectCode(t, br, "501")
	})
}

func TestMultipleRecipients(t *testing.T) {
	sink := &fakeSink{}
	srv := testServer(t, sink, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")

	final := deliver(t, c, br, "alice@example", []string{"one@example", "two@example", "three@example"},
		[]string{"Subject: multi", "", "hello three"})
	if !strings.Contains(final, "stored as msg-1") {
		t.Fatalf("unexpected final reply: %q", final)
	}
	if sink.count() != 1 {
		t.Fatalf("expected exactly 1 stored message, got %d", sink.count())
	}
	got := sink.msgs[0]
	if len(got.to) != 3 || got.to[0] != "one@example" || got.to[2] != "three@example" {
		t.Fatalf("recipients not preserved: %#v", got.to)
	}
}

func TestDotTransparencyAndSingleDotBody(t *testing.T) {
	sink := &fakeSink{}
	srv := testServer(t, sink, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")

	body := []string{
		"Subject: dot test",
		"",
		".",             // body line that is a single dot; wire sends ".."
		"..two dots",    // body line starting with two dots; wire sends "...two dots"
		"...three",      // body line starting with three dots; wire sends "....three"
		"line . inline", // dot not at column 0, untouched
	}
	deliver(t, c, br, "a@example", []string{"b@example"}, body)

	// Exactly one stuffing dot is removed, so content that itself begins
	// with N dots arrives intact beginning with N dots.
	want := "Subject: dot test\r\n" +
		"\r\n" +
		".\r\n" +
		"..two dots\r\n" +
		"...three\r\n" +
		"line . inline\r\n"
	if string(sink.msgs[0].raw) != want {
		t.Fatalf("dot transparency mismatch:\n got: %q\nwant: %q", sink.msgs[0].raw, want)
	}
}

func TestCRLFFraming(t *testing.T) {
	sink := &fakeSink{}
	srv := testServer(t, sink, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	send(t, c, "EHLO x")
	readMultiline(t, br, "250")
	send(t, c, "MAIL FROM:<>") // empty reverse path (bounce-style)
	expectCode(t, br, "250")
	send(t, c, "RCPT TO:<b@example>")
	expectCode(t, br, "250")
	send(t, c, "DATA")
	expectCode(t, br, "354")

	// Send body bytes with explicit CRLF, no trailing newline before marker
	// except the marker's own CRLF.
	if _, err := c.Write([]byte("one\r\ntwo\r\n.\r\n")); err != nil {
		t.Fatalf("write: %v", err)
	}
	expectCode(t, br, "250")
	if string(sink.msgs[0].raw) != "one\r\ntwo\r\n" {
		t.Fatalf("framing mismatch: %q", sink.msgs[0].raw)
	}
	if sink.msgs[0].from != "" {
		t.Fatalf("empty MAIL FROM should be preserved as empty, got %q", sink.msgs[0].from)
	}
}

func TestOversizeMessageRejectedAndNotStored(t *testing.T) {
	sink := &fakeSink{}
	const limit = 128
	srv := testServer(t, sink, limit)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	send(t, c, "EHLO x")
	readMultiline(t, br, "250")
	send(t, c, "MAIL FROM:<a@example>")
	expectCode(t, br, "250")
	send(t, c, "RCPT TO:<b@example>")
	expectCode(t, br, "250")
	send(t, c, "DATA")
	expectCode(t, br, "354")

	// Stream lines until the server reports 552; every line is under the
	// command-length bound but the total exceeds the DATA cap.
	replyErr := make(chan string, 1)
	go func() {
		line, _ := br.ReadString('\n')
		replyErr <- strings.TrimRight(line, "\r\n")
	}()

	big := strings.Repeat("x", 64)
	for i := 0; i < limit; i++ {
		if _, err := fmt.Fprintf(c, "%s\r\n", big); err != nil {
			break
		}
	}
	select {
	case reply := <-replyErr:
		if !strings.HasPrefix(reply, "552") {
			t.Fatalf("expected 552, got %q", reply)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("timed out waiting for 552")
	}
	if sink.count() != 0 {
		t.Fatalf("oversize message must not be stored, got %d", sink.count())
	}
}

func TestDataInterruptStoresNothing(t *testing.T) {
	sink := &fakeSink{}
	srv := testServer(t, sink, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	send(t, c, "EHLO x")
	readMultiline(t, br, "250")
	send(t, c, "MAIL FROM:<a@example>")
	expectCode(t, br, "250")
	send(t, c, "RCPT TO:<b@example>")
	expectCode(t, br, "250")
	send(t, c, "DATA")
	expectCode(t, br, "354")
	send(t, c, "partial line without end marker")
	// Abruptly close mid-DATA, no terminating dot.
	_ = c.Close()

	// Give the handler a moment; absence of save must hold.
	time.Sleep(100 * time.Millisecond)
	if sink.count() != 0 {
		t.Fatalf("interrupted DATA must not be stored, got %d messages", sink.count())
	}
}

func TestRsetClearsTransaction(t *testing.T) {
	sink := &fakeSink{}
	srv := testServer(t, sink, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	send(t, c, "EHLO x")
	readMultiline(t, br, "250")
	send(t, c, "MAIL FROM:<a@example>")
	expectCode(t, br, "250")
	send(t, c, "RCPT TO:<old@example>")
	expectCode(t, br, "250")
	send(t, c, "RSET")
	expectCode(t, br, "250")
	// MAIL now legal without a new EHLO (RSET keeps greeting), but a DATA
	// without fresh RCPT must be rejected.
	send(t, c, "MAIL FROM:<a2@example>")
	expectCode(t, br, "250")
	send(t, c, "DATA")
	expectCode(t, br, "503")
	if sink.count() != 0 {
		t.Fatalf("RSET transaction must not store anything")
	}
}

func TestQuit(t *testing.T) {
	srv := testServer(t, &fakeSink{}, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	send(t, c, "QUIT")
	expectCode(t, br, "221")
	// Server should close the connection.
	one := make([]byte, 1)
	if _, err := c.Read(one); err != io.EOF {
		t.Fatalf("expected EOF after QUIT, got err=%v", err)
	}
}

func TestLongCommandLineRejected(t *testing.T) {
	sink := &fakeSink{}
	srv := testServer(t, sink, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	// A well over the 4096-byte command bound.
	huge := "EHLO " + strings.Repeat("x", 8000)
	send(t, c, "%s", huge)
	expectCode(t, br, "500")
	// Connection still usable for a correctly sized command.
	send(t, c, "EHLO fine")
	readMultiline(t, br, "250")
}

func TestEhloRestartsTransaction(t *testing.T) {
	srv := testServer(t, &fakeSink{}, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	send(t, c, "EHLO x")
	readMultiline(t, br, "250")
	send(t, c, "MAIL FROM:<a@example>")
	expectCode(t, br, "250")
	send(t, c, "EHLO y") // resets envelope
	readMultiline(t, br, "250")
	send(t, c, "RCPT TO:<b@example>")
	expectCode(t, br, "503") // needs MAIL again
}

func TestSinkSaveFailureReturns451(t *testing.T) {
	sink := &fakeSink{fail: true}
	srv := testServer(t, sink, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	send(t, c, "EHLO x")
	readMultiline(t, br, "250")
	send(t, c, "MAIL FROM:<a@example>")
	expectCode(t, br, "250")
	send(t, c, "RCPT TO:<b@example>")
	expectCode(t, br, "250")
	send(t, c, "DATA")
	expectCode(t, br, "354")
	send(t, c, "body")
	send(t, c, ".")
	expectCode(t, br, "451")
	if sink.count() != 0 {
		t.Fatalf("failed save must not be counted")
	}
	// Session stays usable after a transient failure.
	send(t, c, "NOOP")
	expectCode(t, br, "250")
}

func TestInvalidListenAddresses(t *testing.T) {
	cases := []string{
		"0.0.0.0:2525",
		"127.0.0.1",      // missing port
		"localhost:2525", // hostname rejected
		"8.8.8.8:2525",   // non-loopback
		"[::]:2525",      // any IPv6
	}
	for _, addr := range cases {
		_, err := New(Config{Addr: addr, Sink: &fakeSink{}})
		if err == nil {
			t.Errorf("address %q should be rejected", addr)
		}
	}
}

// Integration with the real filesystem store: atomic rename path exercised.
func TestWithRealStore(t *testing.T) {
	dir := t.TempDir()
	st, err := store.Open(filepath.Join(dir, "spool"))
	if err != nil {
		t.Fatalf("store open: %v", err)
	}
	srv := testServer(t, st, 0)
	c, br := dialServer(t, srv.Addr())
	expectCode(t, br, "220")
	deliver(t, c, br, "a@example", []string{"b@example", "c@example"},
		[]string{"Subject: real", "", "stored for real"})

	msgs := st.List()
	if len(msgs) != 1 {
		t.Fatalf("want 1 message, got %d", len(msgs))
	}
	m, err := st.Get(msgs[0].ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if len(m.To) != 2 || !bytes.Contains(m.Raw, []byte("stored for real")) {
		t.Fatalf("stored content wrong: %#v %q", m.To, m.Raw)
	}

	// Reopen: index rebuilt from disk.
	st2, err := store.Open(filepath.Join(dir, "spool"))
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	if len(st2.List()) != 1 {
		t.Fatalf("message not recovered after reopen")
	}
}
