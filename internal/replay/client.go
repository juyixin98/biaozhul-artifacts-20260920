// Package replay is a Modbus TCP test client whose wire behavior is fully
// controllable: individual requests, several ADUs coalesced in one write,
// a single ADU fragmented across many writes with delays, arbitrary raw
// bytes, and FIN/RST disconnects — including halfway through a frame.
package replay

import (
	"errors"
	"fmt"
	"io"
	"net"
	"time"

	"modbus-receipt/internal/protocol"
)

// Conn is one Modbus TCP test connection.
type Conn struct {
	raw net.Conn
}

// Dial opens a connection with the given timeout.
func Dial(addr string, timeout time.Duration) (*Conn, error) {
	c, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return nil, err
	}
	return &Conn{raw: c}, nil
}

// Close closes the connection with a normal FIN (pending data is flushed).
func (c *Conn) Close() error {
	if c.raw == nil {
		return nil
	}
	err := c.raw.Close()
	c.raw = nil
	return err
}

// CloseRST closes with an immediate TCP RST (SO_LINGER zero) instead of a
// FIN, simulating an abrupt client crash.
func (c *Conn) CloseRST() error {
	if tc, ok := c.raw.(*net.TCPConn); ok {
		_ = tc.SetLinger(0)
	}
	return c.Close()
}

// HalfClose sends a FIN while keeping the read side open.
func (c *Conn) HalfClose() error {
	if tc, ok := c.raw.(*net.TCPConn); ok {
		return tc.CloseWrite()
	}
	return errors.New("underlying connection is not TCP")
}

// SetDeadline sets an absolute I/O deadline (0 clears it).
func (c *Conn) SetDeadline(t time.Duration) {
	if t > 0 {
		c.raw.SetDeadline(time.Now().Add(t))
	} else {
		var zero time.Time
		c.raw.SetDeadline(zero)
	}
}

// LocalAddr / RemoteAddr expose the endpoints.
func (c *Conn) RemoteAddr() net.Addr { return c.raw.RemoteAddr() }

// Marshal builds a complete ADU for a PDU.
func Marshal(txn uint16, unit byte, pdu []byte) ([]byte, error) {
	return (&protocol.ADU{TransactionID: txn, ProtocolID: protocol.ProtocolIDTCP, UnitID: unit, PDU: pdu}).Marshal()
}

// Send writes raw bytes verbatim in a single syscall — use it for
// coalesced frames (several ADUs) or for injecting malformed bytes.
func (c *Conn) Send(raw []byte) error {
	_, err := c.raw.Write(raw)
	return err
}

// SendFragmented writes parts one at a time with gap between writes,
// emulating heavy TCP segmentation of one ADU.
func (c *Conn) SendFragmented(parts [][]byte, gap time.Duration) error {
	for i, p := range parts {
		if i > 0 && gap > 0 {
			time.Sleep(gap)
		}
		if _, err := c.raw.Write(p); err != nil {
			return err
		}
	}
	return nil
}

// Fragment splits one ADU into fixed-size pieces for SendFragmented.
func Fragment(adu []byte, size int) [][]byte {
	if size < 1 {
		size = 1
	}
	var out [][]byte
	for len(adu) > 0 {
		n := size
		if n > len(adu) {
			n = len(adu)
		}
		out = append(out, adu[:n])
		adu = adu[n:]
	}
	return out
}

// ReadFrame reads exactly one response ADU.
func (c *Conn) ReadFrame() (*protocol.ADU, error) {
	return protocol.ReadADU(c.raw)
}

// Request sends one well-formed ADU and reads one response.
func (c *Conn) Request(txn uint16, unit byte, pdu []byte) (*protocol.ADU, error) {
	wire, err := Marshal(txn, unit, pdu)
	if err != nil {
		return nil, err
	}
	if err := c.Send(wire); err != nil {
		return nil, err
	}
	return c.ReadFrame()
}

// WriteRegisters performs one FC10 and validates the echo response.
// It returns the raw response ADU plus an error if an exception came back.
func (c *Conn) WriteRegisters(txn uint16, unit byte, addr uint16, values []uint16) (*protocol.ADU, error) {
	resp, err := c.Request(txn, unit, protocol.WriteRequestPDU(addr, values))
	if err != nil {
		return nil, err
	}
	if err := protocol.ParseWriteResponse(resp.PDU, addr, uint16(len(values))); err != nil {
		return resp, err
	}
	return resp, nil
}

// ReadRegisters performs one FC03 and returns the register snapshot.
func (c *Conn) ReadRegisters(txn uint16, unit byte, addr, qty uint16) ([]uint16, *protocol.ADU, error) {
	resp, err := c.Request(txn, unit, protocol.ReadRequestPDU(addr, qty))
	if err != nil {
		return nil, nil, err
	}
	values, err := protocol.ParseReadResponse(resp.PDU)
	return values, resp, err
}

// WriteRegistersFragmented performs one FC10 whose ADU is delivered in
// pieces of fragSize bytes with gap between them — verifies the server's
// length-driven reassembly.
func (c *Conn) WriteRegistersFragmented(txn uint16, unit byte, addr uint16, values []uint16, fragSize int, gap time.Duration) (*protocol.ADU, error) {
	wire, err := Marshal(txn, unit, protocol.WriteRequestPDU(addr, values))
	if err != nil {
		return nil, err
	}
	if err := c.SendFragmented(Fragment(wire, fragSize), gap); err != nil {
		return nil, err
	}
	resp, err := c.ReadFrame()
	if err != nil {
		return nil, err
	}
	if err := protocol.ParseWriteResponse(resp.PDU, addr, uint16(len(values))); err != nil {
		return resp, err
	}
	return resp, nil
}

// SendCoalesced packs several complete ADUs into one write, then reads
// exactly len(adus) responses in order — the TCP "sticky packet" case.
func (c *Conn) SendCoalesced(adus [][]byte) ([]*protocol.ADU, error) {
	var buf []byte
	for _, a := range adus {
		buf = append(buf, a...)
	}
	if err := c.Send(buf); err != nil {
		return nil, err
	}
	resps := make([]*protocol.ADU, len(adus))
	for i := range adus {
		resp, err := c.ReadFrame()
		if err != nil {
			return resps, fmt.Errorf("reading response %d/%d: %w", i+1, len(adus), err)
		}
		resps[i] = resp
	}
	return resps, nil
}

// SendAndAbort writes prefix bytes of a (partial) frame and immediately
// disconnects. how is "fin" or "rst". A compliant server must simply drop
// the connection and never act on the truncated request.
func (c *Conn) SendAndAbort(prefix []byte, how string) error {
	if len(prefix) > 0 {
		if err := c.Send(prefix); err != nil {
			return err
		}
	}
	switch how {
	case "rst":
		return c.CloseRST()
	default:
		// FIN: half-close writes first so no RST is provoked.
		_ = c.HalfClose()
		return c.Close()
	}
}

// WaitEOF reads until the server closes, returning nil on clean EOF.
func (c *Conn) WaitEOF() error {
	buf := make([]byte, 64)
	for {
		_, err := c.raw.Read(buf)
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
	}
}
