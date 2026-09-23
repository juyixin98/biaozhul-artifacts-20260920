package main

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/smtp"
	"strings"
	"testing"
	"time"

	"loopmail/httpapi"
	"loopmail/smtpd"
	"loopmail/store"
)

// startStack wires the real store, SMTP server and HTTP API together on
// ephemeral loopback ports and returns their addresses.
func startStack(t *testing.T, maxBytes int) (smtpAddr, httpAddr string) {
	t.Helper()
	st, err := store.Open(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	srv, err := smtpd.New(smtpd.Config{
		Addr:            "127.0.0.1:0",
		Hostname:        "e2e.local",
		MaxMessageBytes: maxBytes,
		IdleTimeout:     5 * time.Second,
		Sink:            st,
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := srv.Listen(); err != nil {
		t.Fatal(err)
	}
	go func() { _ = srv.Serve() }()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
	})

	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	hs := &http.Server{Handler: httpapi.Handler(st, nil)}
	go func() { _ = hs.Serve(ln) }()
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = hs.Shutdown(ctx)
	})
	return srv.Addr().String(), ln.Addr().String()
}

func TestEndToEndSendAndFetch(t *testing.T) {
	smtpAddr, httpAddr := startStack(t, 0)

	// Standard-library client performs EHLO/MAIL/RCPT/DATA with proper
	// dot-stuffing and CRLF framing.
	c, err := smtp.Dial(smtpAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if err := c.Hello("e2e-client"); err != nil {
		t.Fatal(err)
	}
	if err := c.Mail("sender@example"); err != nil {
		t.Fatal(err)
	}
	for _, rcpt := range []string{"r1@example", "r2@example"} {
		if err := c.Rcpt(rcpt); err != nil {
			t.Fatal(err)
		}
	}
	wc, err := c.Data()
	if err != nil {
		t.Fatal(err)
	}
	body := "Subject: e2e\r\n\r\nline one\r\n.\r\n..dotstuff\r\n"
	if _, err := fmt.Fprint(wc, body); err != nil {
		t.Fatal(err)
	}
	if err := wc.Close(); err != nil {
		t.Fatal(err)
	}
	if err := c.Quit(); err != nil {
		t.Fatal(err)
	}

	// List via HTTP.
	resp, err := http.Get("http://" + httpAddr + "/messages")
	if err != nil {
		t.Fatal(err)
	}
	var list struct {
		Count    int `json:"count"`
		Messages []struct {
			ID      string   `json:"id"`
			To      []string `json:"to"`
			Subject string   `json:"subject"`
		} `json:"messages"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&list); err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if list.Count != 1 || len(list.Messages[0].To) != 2 || list.Messages[0].Subject != "e2e" {
		t.Fatalf("unexpected list: %#v", list)
	}

	// Fetch raw and confirm the dot-stuffed content round-trips.
	resp2, err := http.Get("http://" + httpAddr + "/messages/" + list.Messages[0].ID)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := io.ReadAll(resp2.Body)
	resp2.Body.Close()
	if string(raw) != body {
		t.Fatalf("raw round-trip mismatch:\n got %q\nwant %q", raw, body)
	}
}

func TestEndToEndOversizeRejected(t *testing.T) {
	smtpAddr, _ := startStack(t, 64) // 64-byte cap
	c, err := smtp.Dial(smtpAddr)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if err := c.Hello("x"); err != nil {
		t.Fatal(err)
	}
	if err := c.Mail("a@example"); err != nil {
		t.Fatal(err)
	}
	if err := c.Rcpt("b@example"); err != nil {
		t.Fatal(err)
	}
	wc, err := c.Data()
	if err != nil {
		t.Fatal(err)
	}
	_, _ = fmt.Fprint(wc, strings.Repeat("x", 1000))
	err = wc.Close()
	if err == nil || !strings.Contains(err.Error(), "552") {
		t.Fatalf("expected 552 oversize error, got %v", err)
	}
}
