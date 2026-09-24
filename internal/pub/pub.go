// Package pub is a tiny MQTT v5 publisher helper shared by the simulator and
// test tools. It always publishes with QoS 1.
package pub

import (
	"context"
	"fmt"
	"net"
	"time"

	pahomqtt "github.com/eclipse/paho.golang/paho"
)

// Publisher is a connected MQTT client.
type Publisher struct {
	conn   net.Conn
	client *pahomqtt.Client
}

// Connect dials and performs an MQTT5 connect. cleanStart=false keeps a
// publisher-side durable session too (irrelevant for publishes, but realistic).
func Connect(ctx context.Context, server, clientID string) (*Publisher, error) {
	d := net.Dialer{Timeout: 5 * time.Second}
	conn, err := d.DialContext(ctx, "tcp", server)
	if err != nil {
		return nil, fmt.Errorf("dial: %w", err)
	}
	c := pahomqtt.NewClient(pahomqtt.ClientConfig{
		ClientID: clientID,
		Conn:     conn,
	})
	ca, err := c.Connect(ctx, &pahomqtt.Connect{
		KeepAlive:  30,
		CleanStart: true,
		Properties: &pahomqtt.ConnectProperties{},
	})
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("connect: %w", err)
	}
	if ca.ReasonCode != 0 {
		conn.Close()
		return nil, fmt.Errorf("connect rejected: code=%d", ca.ReasonCode)
	}
	return &Publisher{conn: conn, client: c}, nil
}

// Publish sends one QoS 1 PUBLISH. retained controls the RETAIN flag.
// QoS 1 guarantees the broker accepted the message (PUBACK received).
func (p *Publisher) Publish(ctx context.Context, topic string, payload []byte, retained bool) error {
	pr, err := p.client.Publish(ctx, &pahomqtt.Publish{
		Topic:   topic,
		QoS:     1,
		Retain:  retained,
		Payload: payload,
	})
	if err != nil {
		return err
	}
	if pr != nil && pr.ReasonCode >= 0x80 {
		return fmt.Errorf("publish rejected reason=%d", pr.ReasonCode)
	}
	return nil
}

// Close disconnects.
func (p *Publisher) Close() {
	_ = p.client.Disconnect(&pahomqtt.Disconnect{ReasonCode: 0})
	_ = p.conn.Close()
}
