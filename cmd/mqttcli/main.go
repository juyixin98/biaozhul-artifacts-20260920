// Command mqttcli is a tiny MQTT 3.1.1 subset client used to demonstrate
// the broker's QoS1/ACK/DUP behavior. It is intentionally minimal and
// shares the server's wire codec.
//
// Subcommands:
//
//	pub       connect, publish one QoS1 (or -qos0) message, wait PUBACK
//	sub       connect (durable), subscribe, print inbound PUBLISH frames;
//	          QoS1 messages are PUBACKed unless -hold is given
//	lossdemo  orchestrated acceptance scenario (see runLossDemo)
package main

import (
	"bufio"
	"flag"
	"fmt"
	"log"
	"net"
	"os"
	"time"

	"mqttsub/internal/mqtt"
)

func main() {
	if len(os.Args) < 2 {
		usage()
	}
	switch os.Args[1] {
	case "pub":
		runPub(os.Args[2:])
	case "sub":
		runSub(os.Args[2:])
	case "lossdemo":
		runLossDemo(os.Args[2:])
	default:
		usage()
	}
}

func usage() {
	fmt.Fprintln(os.Stderr, "usage: mqttcli {pub|sub|lossdemo} [flags]")
	os.Exit(2)
}

// client is a minimal synchronous MQTT client.
type client struct {
	nc net.Conn
	br *bufio.Reader
	// autoAck controls PUBACKing of QoS1 PUBLISH frames received while
	// waiting for SUBACK; runSub sets it false under -hold.
	autoAck bool
}

func dial(addr, clientID string, clean bool, keepAlive uint16) (*client, byte, bool, error) {
	nc, err := net.DialTimeout("tcp", addr, 3*time.Second)
	if err != nil {
		return nil, 0, false, err
	}
	c := &client{nc: nc, br: bufio.NewReader(nc), autoAck: true}
	conn := &mqtt.ConnectPacket{ClientID: clientID, CleanSession: clean, KeepAlive: keepAlive}
	if _, err := nc.Write(mqtt.EncodeConnect(conn)); err != nil {
		nc.Close()
		return nil, 0, false, err
	}
	fr, err := mqtt.ReadFrame(c.br)
	if err != nil {
		nc.Close()
		return nil, 0, false, err
	}
	if fr.Type() != mqtt.TypeCONNACK || len(fr.Body) != 2 {
		nc.Close()
		return nil, 0, false, fmt.Errorf("bad CONNACK")
	}
	return c, fr.Body[1], fr.Body[0] == 1, nil
}

func (c *client) sendRaw(p []byte) {
	if _, err := c.nc.Write(p); err != nil {
		log.Fatalf("write: %v", err)
	}
}

func (c *client) close(clean bool) {
	if clean {
		_, _ = c.nc.Write(mqtt.PacketDisconnect)
	}
	_ = c.nc.Close()
}

func (c *client) subscribe(filter string, maxQoS byte, pid uint16) error {
	_, err := c.nc.Write(mqtt.EncodeSubscribe(pid, []mqtt.Subscription{{Filter: filter, MaxQoS: maxQoS}}))
	if err != nil {
		return err
	}
	// The broker may start redelivering inflight messages immediately
	// after CONNACK, so a PUBLISH can arrive before SUBACK. Drain frames
	// until the SUBACK shows up, auto-acking QoS1 along the way.
	for {
		fr, err := mqtt.ReadFrame(c.br)
		if err != nil {
			return err
		}
		switch fr.Type() {
		case mqtt.TypeSUBACK:
			return nil
		case mqtt.TypePUBLISH:
			p, perr := mqtt.DecodePublish(fr.Flags(), fr.Body)
			if perr != nil {
				return perr
			}
			fmt.Printf("RECV DUP=%v QoS=%d pid=%d topic=%s payload=%q (before SUBACK)\n",
				p.Dup, p.QoS, p.PacketID, p.Topic, string(p.Payload))
			if p.QoS == 1 && c.autoAck {
				c.sendRaw(mqtt.EncodePuback(p.PacketID))
			}
		default:
			return fmt.Errorf("unexpected type %d while awaiting SUBACK", fr.Type())
		}
	}
}

func (c *client) publishQoS1(topic string, payload []byte, pid uint16) error {
	pkt := mqtt.EncodePublish(&mqtt.PublishPacket{
		QoS: 1, Topic: topic, Payload: payload, PacketID: pid,
	})
	if _, err := c.nc.Write(pkt); err != nil {
		return err
	}
	fr, err := mqtt.ReadFrame(c.br)
	if err != nil {
		return err
	}
	if fr.Type() != mqtt.TypePUBACK {
		return fmt.Errorf("expected PUBACK, got type %d", fr.Type())
	}
	return nil
}

func runPub(args []string) {
	fs := flag.NewFlagSet("pub", flag.ExitOnError)
	addr := fs.String("addr", "127.0.0.1:1883", "broker address")
	id := fs.String("id", "pub-1", "client id")
	topic := fs.String("topic", "demo/topic", "topic")
	msg := fs.String("msg", "hello", "payload")
	qos0 := fs.Bool("qos0", false, "publish as QoS0")
	clean := fs.Bool("clean", true, "clean session")
	_ = fs.Parse(args)

	c, code, _, err := dial(*addr, *id, *clean, 20)
	if err != nil {
		log.Fatalf("connect: %v", err)
	}
	if code != 0 {
		log.Fatalf("CONNECT rejected, code %d", code)
	}
	defer c.close(true)

	if *qos0 {
		pkt := mqtt.EncodePublish(&mqtt.PublishPacket{QoS: 0, Topic: *topic, Payload: []byte(*msg)})
		_, _ = c.nc.Write(pkt)
		fmt.Printf("published QoS0 -> %s\n", *topic)
		return
	}
	if err := c.publishQoS1(*topic, []byte(*msg), 1); err != nil {
		log.Fatalf("publish: %v", err)
	}
	fmt.Printf("published QoS1 (pid=1), PUBACK received -> %s\n", *topic)
}

func runSub(args []string) {
	fs := flag.NewFlagSet("sub", flag.ExitOnError)
	addr := fs.String("addr", "127.0.0.1:1883", "broker address")
	id := fs.String("id", "sub-1", "client id")
	filter := fs.String("filter", "#", "topic filter")
	clean := fs.Bool("clean", false, "clean session (default durable)")
	hold := fs.Bool("hold", false, "do not send PUBACK (simulate slow/lost ack)")
	duration := fs.Duration("duration", 10*time.Second, "how long to listen")
	_ = fs.Parse(args)

	c, code, sp, err := dial(*addr, *id, *clean, 30)
	if err != nil {
		log.Fatalf("connect: %v", err)
	}
	if code != 0 {
		log.Fatalf("CONNECT rejected, code %d", code)
	}
	fmt.Printf("connected id=%s clean=%v sessionPresent=%v\n", *id, *clean, sp)
	c.autoAck = !*hold
	if err := c.subscribe(*filter, 1, 1); err != nil {
		log.Fatalf("subscribe: %v", err)
	}
	fmt.Printf("subscribed filter=%s (QoS1), listening for %s\n", *filter, *duration)

	deadline := time.Now().Add(*duration)
	for {
		_ = c.nc.SetReadDeadline(deadline)
		fr, err := mqtt.ReadFrame(c.br)
		if err != nil {
			break
		}
		switch fr.Type() {
		case mqtt.TypePUBLISH:
			p, perr := mqtt.DecodePublish(fr.Flags(), fr.Body)
			if perr != nil {
				log.Fatalf("bad PUBLISH: %v", perr)
			}
			fmt.Printf("RECV DUP=%v QoS=%d pid=%d topic=%s payload=%q\n",
				p.Dup, p.QoS, p.PacketID, p.Topic, string(p.Payload))
			if p.QoS == 1 && !*hold {
				_, _ = c.nc.Write(mqtt.EncodePuback(p.PacketID))
				fmt.Printf("  -> sent PUBACK pid=%d\n", p.PacketID)
			}
			if p.QoS == 1 && *hold {
				fmt.Printf("  -> holding back PUBACK pid=%d\n", p.PacketID)
			}
		case mqtt.TypePINGRESP:
			// keep-alive response; ignore
		default:
			fmt.Printf("recv unexpected type %d\n", fr.Type())
		}
	}
	// Clean disconnect unless we are deliberately leaving the durable
	// session around (typical for offline-queue demos).
	c.close(!*clean)
	if *clean {
		fmt.Println("disconnected cleanly (clean session discarded)")
	} else {
		fmt.Println("TCP closed WITHOUT DISCONNECT: durable session retained (simulated crash)")
	}
}
