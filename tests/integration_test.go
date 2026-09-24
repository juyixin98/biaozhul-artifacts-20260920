package integration_test

import (
	"net/smtp"
	"path/filepath"
	"testing"
	"time"

	"smtprecv/internal/smtpd"
	"smtprecv/internal/store"
)

// TestStandardLibraryClient drives the server with Go's net/smtp client,
// which speaks real CRLF framing and DATA dot-stuffing itself.
func TestStandardLibraryClient(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "mail")
	st, _ := store.Open(dir)
	srv, err := smtpd.New(smtpd.Config{
		ListenAddr:     "127.0.0.1:0",
		Hostname:       "localhost",
		MaxMessageSize: 1 << 20,
		CommandTimeout: 5 * time.Second,
	}, st)
	if err != nil {
		t.Fatal(err)
	}
	if err := srv.Start(); err != nil {
		t.Fatal(err)
	}
	defer srv.Close()

	body := "Subject: integration\r\n" +
		"\r\n" +
		"a line starting with a dot: .hello\r\n" +
		"literal dot line:\r\n" +
		".\r\n" + // the client must dot-stuff this to ".."
		"end\r\n"

	err = smtp.SendMail(
		srv.Addr().String(),
		nil,
		"alice@example.com",
		[]string{"bob@example.com", "carol@example.com"},
		[]byte(body),
	)
	if err != nil {
		t.Fatalf("SendMail: %v", err)
	}

	msgs := st.List(0)
	if len(msgs) != 1 {
		t.Fatalf("want 1 message, got %d", len(msgs))
	}
	m, _ := st.Get(msgs[0].ID)
	if m.From != "alice@example.com" || len(m.To) != 2 {
		t.Fatalf("envelope wrong: %+v", m)
	}
	if m.Data != body {
		t.Errorf("round-trip body mismatch:\n got %q\nwant %q", m.Data, body)
	}
}
