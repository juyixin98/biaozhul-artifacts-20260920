// Command mqttdemo is a tiny scriptable MQTT 3.1.1 subset client used to
// demonstrate the acceptance scenarios by hand. It speaks only this broker's
// subset and is intentionally minimal.
//
// Usage:
//
//	mqttdemo sub   -addr 127.0.0.1:1883 -id sub1 -filter a/b -qos 1 -noack
//	mqttdemo pub   -addr 127.0.0.1:1883 -id pub1 -topic a/b -qos 1 -msg hello -pid 1 [-dup]
//	mqttdemo ping  -addr 127.0.0.1:1883 -id pinger -keepalive 10
//
// Add -clean for CleanSession=1; persistent (-clean omitted) subscribers keep
// their session between runs, so restarting "sub" exercises redelivery.
package main

import (
	"encoding/binary"
	"flag"
	"fmt"
	"io"
	"net"
	"os"
	"time"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	cmd := os.Args[1]

	fs := flag.NewFlagSet(cmd, flag.ExitOnError)
	addr := fs.String("addr", "127.0.0.1:1883", "broker address")
	id := fs.String("id", "demo", "client id")
	clean := fs.Bool("clean", false, "set CleanSession=1")
	keepalive := fs.Int("keepalive", 0, "keep alive seconds")
	filter := fs.String("filter", "#", "sub: topic filter")
	qos := fs.Int("qos", 1, "sub/pub: qos 0 or 1")
	noack := fs.Bool("noack", false, "sub: never PUBACK (simulates permanent ack loss)")
	topic := fs.String("topic", "demo", "pub: topic")
	msg := fs.String("msg", "", "pub: payload text")
	pid := fs.Int("pid", 1, "pub: packet identifier (qos 1)")
	dup := fs.Bool("dup", false, "pub: set the DUP flag")
	_ = fs.Parse(os.Args[2:])

	switch cmd {
	case "sub":
		runSub(*addr, *id, *clean, uint16(*keepalive), *filter, *qos, *noack)
	case "pub":
		runPub(*addr, *id, *clean, uint16(*keepalive), *topic, *msg, *qos, uint16(*pid), *dup)
	case "ping":
		runPing(*addr, *id, *clean, uint16(*keepalive))
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: mqttdemo {sub|pub|ping} [flags]")
	os.Exit(2)
}

type client struct{ raw net.Conn }

func dialConnect(addr, id string, clean bool, ka uint16) (*client, bool) {
	raw, err := net.DialTimeout("tcp", addr, 3*time.Second)
	if err != nil {
		fail("dial: %v", err)
	}
	c := &client{raw: raw}
	c.write(connectFrame(id, clean, ka))
	first, body := c.read()
	if first != 0x20 || len(body) != 2 {
		fail("expected CONNACK, got %02x % x", first, body)
	}
	if body[1] != 0 {
		fail("CONNACK return code %d", body[1])
	}
	return c, body[0] == 1
}

func connectFrame(id string, clean bool, ka uint16) []byte {
	var flags byte
	if clean {
		flags = 0x02
	}
	body := str16(nil, "MQTT")
	body = append(body, 4, flags, byte(ka>>8), byte(ka))
	body = str16(body, id)
	out := []byte{0x10}
	return append(out, append(remLen(nil, len(body)), body...)...)
}

func str16(dst []byte, s string) []byte {
	dst = binary.BigEndian.AppendUint16(dst, uint16(len(s)))
	return append(dst, s...)
}

func remLen(dst []byte, n int) []byte {
	for {
		b := byte(n % 128)
		n /= 128
		if n > 0 {
			b |= 0x80
		}
		dst = append(dst, b)
		if n == 0 {
			return dst
		}
	}
}

func (c *client) write(b []byte) {
	if _, err := c.raw.Write(b); err != nil {
		fail("write: %v", err)
	}
}

func (c *client) read() (byte, []byte) {
	hdr := []byte{0}
	if _, err := io.ReadFull(c.raw, hdr); err != nil {
		fail("read header: %v", err)
	}
	var rem, mult int
	mult = 1
	for i := 0; i < 4; i++ {
		b := []byte{0}
		if _, err := io.ReadFull(c.raw, b); err != nil {
			fail("read remlen: %v", err)
		}
		rem += int(b[0]&0x7f) * mult
		if b[0]&0x80 == 0 {
			break
		}
		mult *= 128
	}
	body := make([]byte, rem)
	if _, err := io.ReadFull(c.raw, body); err != nil {
		fail("read body: %v", err)
	}
	return hdr[0], body
}

func runSub(addr, id string, clean bool, ka uint16, filter string, qos int, noack bool) {
	c, sp := dialConnect(addr, id, clean, ka)
	fmt.Printf("connected; session-present=%v\n", sp)

	// Read loop runs immediately: on a persistent reconnect the server may
	// start redelivering inflight PUBLISH packets before (or while) we send
	// SUBSCRIBE, which is legal per MQTT 3.1.1.
	gotSuback := make(chan struct{})
	go func() {
		for {
			_ = c.raw.SetReadDeadline(time.Time{})
			first, pbody := c.read()
			switch first >> 4 {
			case 3: // PUBLISH
				q := (first >> 1) & 3
				d := first&0x08 != 0
				tl := int(binary.BigEndian.Uint16(pbody))
				topic := string(pbody[2 : 2+tl])
				pos := 2 + tl
				var mpid uint16
				if q > 0 {
					mpid = binary.BigEndian.Uint16(pbody[pos : pos+2])
					pos += 2
				}
				fmt.Printf("PUBLISH dup=%v qos=%d id=%d topic=%s payload=%q\n",
					d, q, mpid, topic, string(pbody[pos:]))
				if q == 1 && !noack {
					c.write([]byte{0x40, 0x02, byte(mpid >> 8), byte(mpid)})
				}
			case 9: // SUBACK
				fmt.Printf("SUBACK granted qos %d\n", pbody[len(pbody)-1])
				select {
				case <-gotSuback:
				default:
					close(gotSuback)
				}
			case 13: // PINGRESP
			default:
				fmt.Printf("unexpected packet type %d\n", first>>4)
			}
		}
	}()

	body := binary.BigEndian.AppendUint16(nil, 1)
	body = str16(body, filter)
	body = append(body, byte(qos))
	c.write(append([]byte{0x82}, append(remLen(nil, len(body)), body...)...))
	<-gotSuback
	fmt.Printf("subscribed to %s\n", filter)
	select {} // block until the process is killed
}

func runPub(addr, id string, clean bool, ka uint16, topic, msg string, qos int, pidv uint16, dup bool) {
	c, sp := dialConnect(addr, id, clean, ka)
	fmt.Printf("connected; session-present=%v\n", sp)

	var first byte = 0x30
	if dup {
		first |= 0x08
	}
	first |= byte(qos) << 1
	vh := str16(nil, topic)
	if qos == 1 {
		vh = binary.BigEndian.AppendUint16(vh, pidv)
	}
	out := []byte{first}
	out = append(out, remLen(nil, len(vh)+len(msg))...)
	out = append(out, vh...)
	out = append(out, msg...)
	c.write(out)
	if qos == 1 {
		f, pb := c.read()
		if f>>4 != 4 || binary.BigEndian.Uint16(pb) != pidv {
			fail("bad PUBACK: %02x % x", f, pb)
		}
		fmt.Printf("PUBACK id=%d\n", pidv)
	}
	c.write([]byte{0xE0, 0x00})
	_ = c.raw.Close()
}

func runPing(addr, id string, clean bool, ka uint16) {
	c, sp := dialConnect(addr, id, clean, ka)
	fmt.Printf("connected; session-present=%v\n", sp)
	for i := 1; ; i++ {
		c.write([]byte{0xC0, 0x00})
		if f, _ := c.read(); f != 0xD0 {
			fail("expected PINGRESP, got %02x", f)
		}
		fmt.Printf("PINGRESP #%d\n", i)
		if ka > 0 {
			time.Sleep(time.Duration(ka) * time.Second * 2 / 3)
		} else {
			time.Sleep(time.Second)
		}
	}
}

func fail(format string, args ...any) {
	fmt.Fprintf(os.Stderr, "error: "+format+"\n", args...)
	os.Exit(1)
}
