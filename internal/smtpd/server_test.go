package smtpd_test

import (
	"bufio"
	"fmt"
	"io"
	"net"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"smtprecv/internal/smtpd"
	"smtprecv/internal/store"
)

type testServer struct {
	t    *testing.T
	st   *store.Store
	srv  *smtpd.Server
	addr string
}

func startTestServer(t *testing.T, maxSize int) *testServer {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "mail")
	st, err := store.Open(dir)
	if err != nil {
		t.Fatalf("store open: %v", err)
	}
	srv, err := smtpd.New(smtpd.Config{
		ListenAddr:     "127.0.0.1:0",
		Hostname:       "localhost",
		MaxMessageSize: maxSize,
		MaxRecipients:  100,
		CommandTimeout: time.Second,
	}, st)
	if err != nil {
		t.Fatalf("smtpd new: %v", err)
	}
	if err := srv.Start(); err != nil {
		t.Fatalf("smtpd start: %v", err)
	}
	t.Cleanup(func() { srv.Close() })
	return &testServer{t: t, st: st, srv: srv, addr: srv.Addr().String()}
}

func (ts *testServer) dial() *smtpConn {
	ts.t.Helper()
	c, err := net.Dial("tcp", ts.addr)
	if err != nil {
		ts.t.Fatalf("dial: %v", err)
	}
	sc := &smtpConn{t: ts.t, c: c, r: bufio.NewReader(c)}
	ts.t.Cleanup(func() { c.Close() })
	greet := sc.readReply()
	if !strings.HasPrefix(greet, "220 ") {
		ts.t.Fatalf("bad greeting: %q", greet)
	}
	return sc
}

type smtpConn struct {
	t *testing.T
	c net.Conn
	r *bufio.Reader
}

func (sc *smtpConn) cmd(f string, a ...any) string {
	sc.t.Helper()
	fmt.Fprintf(sc.c, f+"\r\n", a...)
	return sc.readReply()
}

// raw writes exactly what it is given, no CRLF appended.
func (sc *smtpConn) raw(s string) {
	sc.t.Helper()
	if _, err := io.WriteString(sc.c, s); err != nil {
		sc.t.Fatalf("raw write: %v", err)
	}
}

func (sc *smtpConn) readReply() string {
	sc.t.Helper()
	var lines []string
	for {
		line, err := sc.r.ReadString('\n')
		if err != nil {
			sc.t.Fatalf("read reply: %v", err)
		}
		line = strings.TrimRight(line, "\r\n")
		lines = append(lines, line)
		if len(line) >= 4 && line[3] == ' ' {
			break
		}
	}
	return strings.Join(lines, "\n")
}

// expectCode reads a reply and asserts its 3-digit status code.
func (sc *smtpConn) expectCode(want, got string) {
	sc.t.Helper()
	if len(got) < 3 || got[:3] != want {
		sc.t.Errorf("want code %s, got %q", want, got)
	}
}

func (sc *smtpConn) close() { sc.c.Close() }

func TestFullMessageFlow(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()

	c.expectCode("250", c.cmd("EHLO tester"))
	c.expectCode("250", c.cmd("MAIL FROM:<alice@example.com>"))
	c.expectCode("250", c.cmd("RCPT TO:<bob@example.com>"))
	c.expectCode("354", c.cmd("DATA"))
	c.raw("Subject: hello\r\n\r\nbody line one\r\n.\r\n")
	c.expectCode("250", c.readReply())
	c.expectCode("221", c.cmd("QUIT"))

	msgs := ts.st.List(0)
	if len(msgs) != 1 {
		t.Fatalf("want 1 stored message, got %d", len(msgs))
	}
	full, err := ts.st.Get(msgs[0].ID)
	if err != nil {
		t.Fatalf("get: %v", err)
	}
	if full.From != "alice@example.com" {
		t.Errorf("from = %q", full.From)
	}
	if want := "Subject: hello\r\n\r\nbody line one\r\n"; full.Data != want {
		t.Errorf("data = %q, want %q", full.Data, want)
	}
}

func TestOutOfOrderCommands(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()

	// No EHLO yet.
	c.expectCode("503", c.cmd("MAIL FROM:<a@x>"))
	c.expectCode("503", c.cmd("RCPT TO:<b@x>"))
	c.expectCode("503", c.cmd("DATA"))

	c.expectCode("250", c.cmd("EHLO tester"))

	// MAIL before nothing fine, but RCPT/DATA need the envelope in order.
	c.expectCode("250", c.cmd("MAIL FROM:<a@x>"))
	c2 := ts.dial()
	c2.expectCode("250", c2.cmd("EHLO other"))
	c2.expectCode("503", c2.cmd("DATA"))          // no MAIL/RCPT
	c2.expectCode("503", c2.cmd("RCPT TO:<b@x>")) // no MAIL
	c2.close()

	c.expectCode("503", c.cmd("DATA")) // no RCPT yet
	c.expectCode("250", c.cmd("RCPT TO:<b@x>"))
	c.expectCode("354", c.cmd("DATA"))
	c.raw(".\r\n")
	c.expectCode("250", c.readReply())

	if n := len(ts.st.List(0)); n != 1 {
		t.Fatalf("want 1 message, got %d", n)
	}
}

func TestMultipleRecipients(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.cmd("EHLO tester")
	c.cmd("MAIL FROM:<alice@example.com>")
	for _, r := range []string{"bob@example.com", "carol@example.com", "dave@example.com"} {
		c.expectCode("250", c.cmd("RCPT TO:<%s>", r))
	}
	c.expectCode("354", c.cmd("DATA"))
	c.raw("multi-recipient test\r\n.\r\n")
	c.expectCode("250", c.readReply())

	msgs := ts.st.List(0)
	if len(msgs) != 1 {
		t.Fatalf("want 1 message, got %d", len(msgs))
	}
	m, _ := ts.st.Get(msgs[0].ID)
	if len(m.To) != 3 {
		t.Fatalf("want 3 recipients, got %v", m.To)
	}
	if m.To[0] != "bob@example.com" || m.To[1] != "carol@example.com" || m.To[2] != "dave@example.com" {
		t.Errorf("recipients = %v", m.To)
	}
}

func TestDotTransparencyAndSingleDot(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.cmd("EHLO tester")
	c.cmd("MAIL FROM:<a@x>")
	c.cmd("RCPT TO:<b@x>")
	c.expectCode("354", c.cmd("DATA"))
	// Body: a leading-dot line, a literal "." line transmitted as "..",
	// a line that begins with "....", and a normal line.
	c.raw("..leading dot\r\n")
	c.raw("...two dots\r\n")
	c.raw("normal\r\n")
	c.raw(".\r\n")
	c.expectCode("250", c.readReply())

	m, _ := ts.st.Get(ts.st.List(0)[0].ID)
	want := ".leading dot\r\n..two dots\r\nnormal\r\n"
	if m.Data != want {
		t.Errorf("dot transparency failed:\n got %q\nwant %q", m.Data, want)
	}
}

func TestOversizeMessageRejectedAndNotStored(t *testing.T) {
	const limit = 1024
	ts := startTestServer(t, limit)
	c := ts.dial()
	c.cmd("EHLO tester")
	c.cmd("MAIL FROM:<a@x>")
	c.cmd("RCPT TO:<b@x>")
	c.expectCode("354", c.cmd("DATA"))

	big := strings.Repeat("x", limit+100)
	fmt.Fprintf(c.c, "%s\r\n", big)
	c.raw(".\r\n")
	c.expectCode("552", c.readReply())

	if n := len(ts.st.List(0)); n != 0 {
		t.Fatalf("oversize message stored; count = %d", n)
	}

	// Session stays usable: a small follow-up message works.
	c.cmd("MAIL FROM:<a@x>") // oversize aborted the transaction (RSET-like)
	c.cmd("RCPT TO:<b@x>")
	c.expectCode("354", c.cmd("DATA"))
	c.raw("small\r\n.\r\n")
	c.expectCode("250", c.readReply())
	if n := len(ts.st.List(0)); n != 1 {
		t.Fatalf("want 1 message after small follow-up, got %d", n)
	}
}

func TestDataInterruptNotStored(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.cmd("EHLO tester")
	c.cmd("MAIL FROM:<a@x>")
	c.cmd("RCPT TO:<b@x>")
	c.expectCode("354", c.cmd("DATA"))
	c.raw("incomplete body without terminator")
	c.close() // TCP drop mid-DATA

	// Give the server a moment to notice; nothing must ever be stored.
	deadline := time.Now().Add(time.Second)
	for time.Now().Before(deadline) {
		if len(ts.st.List(0)) != 0 {
			t.Fatal("partial message was stored after DATA interruption")
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestRSETClearsEnvelope(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.cmd("EHLO tester")
	c.cmd("MAIL FROM:<a@x>")
	c.cmd("RCPT TO:<b@x>")
	c.expectCode("250", c.cmd("RSET"))
	c.expectCode("503", c.cmd("DATA"))          // RCPT cleared
	c.expectCode("503", c.cmd("RCPT TO:<b@x>")) // MAIL cleared too
	c.cmd("MAIL FROM:<a@x>")
	c.cmd("RCPT TO:<b@x>")
	c.expectCode("354", c.cmd("DATA"))
	c.raw("after reset\r\n.\r\n")
	c.expectCode("250", c.readReply())
	if n := len(ts.st.List(0)); n != 1 {
		t.Fatalf("want 1 message, got %d", n)
	}
}

func TestBareLFRejectedForCommands(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.raw("EHLO tester\n") // bare LF, not CRLF
	c.expectCode("501", c.readReply())
	// Strict framing; still synchronized: a proper command works next.
	c.expectCode("250", c.cmd("EHLO tester"))
}

func TestBareLFToleratedInDataBody(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.cmd("EHLO tester")
	c.cmd("MAIL FROM:<a@x>")
	c.cmd("RCPT TO:<b@x>")
	c.expectCode("354", c.cmd("DATA"))
	c.raw("line one\nline two\n.\n") // bare-LF framed body
	c.expectCode("250", c.readReply())
	m, _ := ts.st.Get(ts.st.List(0)[0].ID)
	// Stored normalized to CRLF; framing CRLF before the dot is removed.
	want := "line one\r\nline two\r\n"
	if m.Data != want {
		t.Errorf("body = %q, want %q", m.Data, want)
	}
}

func TestSizeParameterOnMail(t *testing.T) {
	const limit = 1024
	ts := startTestServer(t, limit)
	c := ts.dial()
	c.cmd("EHLO tester")
	c.expectCode("552", c.cmd("MAIL FROM:<a@x> SIZE=%d", limit+1))
	c.expectCode("250", c.cmd("MAIL FROM:<a@x> SIZE=512"))
}

func TestHELOWorks(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	reply := c.cmd("HELO tester")
	if !strings.HasPrefix(reply, "250 ") || strings.Contains(reply, "-") {
		t.Errorf("HELO should get a single 250 line, got %q", reply)
	}
}

func TestUnknownAndEmptyCommands(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.expectCode("500", c.cmd(""))
	c.expectCode("500", c.cmd("FROBNICATE"))
	c.expectCode("502", c.cmd("VRFY a@x"))
	c.expectCode("250", c.cmd("NOOP"))
}

func TestEHLOAdvertisesSize(t *testing.T) {
	ts := startTestServer(t, 4096)
	c := ts.dial()
	reply := c.cmd("EHLO tester")
	if !strings.Contains(reply, "250-SIZE 4096") {
		t.Errorf("EHLO did not advertise size limit:\n%s", reply)
	}
}

func TestFailedEHLONotAcceptedAsGreeting(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.expectCode("501", c.cmd("EHLO"))
	// State must remain ungreeted.
	c.expectCode("503", c.cmd("MAIL FROM:<a@x>"))
	c.expectCode("250", c.cmd("EHLO tester"))
	c.expectCode("250", c.cmd("MAIL FROM:<a@x>"))
}

func TestCommandLineTooLong(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	// 12 KiB command, spanning multiple bufio buffers: exactly one 500, and
	// the session must still accept a following well-formed command.
	c.raw("EHLO " + strings.Repeat("x", 12*1024) + "\r\n")
	c.expectCode("500", c.readReply())
	c.expectCode("250", c.cmd("EHLO tester"))
}

func TestFragmentedWrites(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	for _, piece := range []string{"EH", "LO tes", "ter\r\n"} {
		c.raw(piece)
		time.Sleep(5 * time.Millisecond)
	}
	c.expectCode("250", c.readReply())
	c.raw("MAIL")
	time.Sleep(5 * time.Millisecond)
	c.raw(" FROM:<a@x>\r\n")
	c.expectCode("250", c.readReply())
}

func TestIdleTimeout(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.cmd("EHLO tester")
	reply := c.readReply421OrEOF()
	if !strings.HasPrefix(reply, "421 ") {
		t.Errorf("want 421 on idle timeout, got %q", reply)
	}
}

func (sc *smtpConn) readReply421OrEOF() string {
	line, err := sc.r.ReadString('\n')
	if err != nil {
		return "EOF"
	}
	return strings.TrimRight(line, "\r\n")
}

func TestNonLoopbackBindRejected(t *testing.T) {
	st, _ := store.Open(t.TempDir())
	_, err := smtpd.New(smtpd.Config{ListenAddr: "0.0.0.0:2525"}, st)
	if err == nil {
		t.Fatal("expected refusal to bind 0.0.0.0")
	}
}

func TestDuplicateRecipientIgnored(t *testing.T) {
	ts := startTestServer(t, 1<<20)
	c := ts.dial()
	c.cmd("EHLO tester")
	c.cmd("MAIL FROM:<a@x>")
	c.cmd("RCPT TO:<b@x>")
	c.expectCode("250", c.cmd("RCPT TO:<B@x>")) // case-insensitive dup
	c.expectCode("354", c.cmd("DATA"))
	c.raw(".\r\n")
	c.expectCode("250", c.readReply())
	m, _ := ts.st.Get(ts.st.List(0)[0].ID)
	if len(m.To) != 1 {
		t.Errorf("want 1 deduplicated recipient, got %v", m.To)
	}
}
