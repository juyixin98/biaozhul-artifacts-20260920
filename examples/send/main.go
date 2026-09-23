// Command sendmail is a small example SMTP client for loopmail, written with
// the standard library's net/smtp. It demonstrates a full transaction —
// including a body that contains a line of just "." (dot transparency).
//
// Usage:
//
//	go run ./examples/send -addr 127.0.0.1:2525
package main

import (
	"flag"
	"fmt"
	"log"
	"net/smtp"
)

func main() {
	addr := flag.String("addr", "127.0.0.1:2525", "loopmail SMTP address")
	from := flag.String("from", "alice@example.test", "envelope sender")
	flag.Parse()

	// Multiple recipients are all accepted; the receiver stores one message
	// carrying every RCPT TO address.
	to := []string{"bob@example.test", "carol@example.test"}

	// net/smtp.Data() transparently dot-stuffs content lines, so a body
	// containing a bare "." line is sent as ".." and reconstructed on the
	// server.
	msg := []byte("From: alice@example.test\r\n" +
		"To: bob@example.test, carol@example.test\r\n" +
		"Subject: Dot transparency demo\r\n" +
		"\r\n" +
		"First line.\r\n" +
		".\r\n" + // a line containing a single dot
		"..starts with two dots\r\n" +
		"Last line.\r\n")

	c, err := smtp.Dial(*addr)
	if err != nil {
		log.Fatalf("dial %s: %v", *addr, err)
	}
	defer c.Close()
	if err := c.Hello("example-sender"); err != nil {
		log.Fatalf("EHLO: %v", err)
	}
	if err := c.Mail(*from); err != nil {
		log.Fatalf("MAIL: %v", err)
	}
	for _, rcpt := range to {
		if err := c.Rcpt(rcpt); err != nil {
			log.Fatalf("RCPT %s: %v", rcpt, err)
		}
	}
	w, err := c.Data()
	if err != nil {
		log.Fatalf("DATA: %v", err)
	}
	if _, err := w.Write(msg); err != nil {
		log.Fatalf("write body: %v", err)
	}
	if err := w.Close(); err != nil {
		log.Fatalf("end DATA: %v", err)
	}
	if err := c.Quit(); err != nil {
		log.Fatalf("QUIT: %v", err)
	}
	fmt.Println("sent OK to", to)
}
