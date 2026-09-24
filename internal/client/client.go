// Package client is a small Modbus/TCP client used by the `modbusctl`
// command-line tool and by the replay file runner.
package client

import (
	"encoding/binary"
	"errors"
	"fmt"
	"io"
	"net"
	"sync"
	"time"

	"modbus-receipt/internal/protocol"
)

// ErrTransactionMismatch means a response's Transaction ID did not match the
// request. Modbus/TCP matches responses to requests with the Transaction
// Identifier; a mismatch on an ordered connection is a protocol violation.
var ErrTransactionMismatch = errors.New("modbus: response transaction id mismatch")

// ExceptionError is a Modbus exception response (FC|0x80).
type ExceptionError struct {
	FunctionCode  byte
	ExceptionCode byte
}

func (e *ExceptionError) Error() string {
	name := protocol.ExceptionText[e.ExceptionCode]
	if name == "" {
		name = "unknown exception"
	}
	return fmt.Sprintf("modbus exception 0x%02X (%s) on function 0x%02X",
		e.ExceptionCode, name, e.FunctionCode)
}

// Client talks Modbus/TCP over one connection. Safe for concurrent use.
type Client struct {
	conn net.Conn
	mu   sync.Mutex
	next uint16
}

// Dial opens a connection to addr (host:port).
func Dial(addr string, timeout time.Duration) (*Client, error) {
	conn, err := net.DialTimeout("tcp", addr, timeout)
	if err != nil {
		return nil, fmt.Errorf("modbus: dial %s: %w", addr, err)
	}
	return &Client{conn: conn, next: 1}, nil
}

// New wraps an existing connection (used by tests to inject raw streams).
func New(conn net.Conn) *Client {
	return &Client{conn: conn, next: 1}
}

// Close closes the underlying connection.
func (c *Client) Close() error { return c.conn.Close() }

// Conn exposes the raw connection for tests that craft malformed traffic.
func (c *Client) Conn() net.Conn { return c.conn }

// RoundTrip sends one frame and reads one response, checking the Transaction
// ID. The caller supplies the exact PDU; DoFC03/DoFC10 are convenience
// wrappers.
func (c *Client) RoundTrip(txnID uint16, unitID byte, pdu []byte) (protocol.Frame, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	req := protocol.Frame{
		TransactionID: txnID,
		ProtocolID:    protocol.ProtocolIDModbus,
		UnitID:        unitID,
		PDU:           pdu,
	}
	if _, err := c.conn.Write(req.Encode()); err != nil {
		return protocol.Frame{}, fmt.Errorf("modbus: write request: %w", err)
	}
	resp, err := protocol.ReadFrame(c.conn)
	if err != nil {
		return protocol.Frame{}, fmt.Errorf("modbus: read response: %w", err)
	}
	if resp.TransactionID != txnID {
		return protocol.Frame{}, fmt.Errorf("%w: sent %d got %d",
			ErrTransactionMismatch, txnID, resp.TransactionID)
	}
	if code, isExc := protocol.AsException(resp.PDU); isExc {
		return resp, &ExceptionError{FunctionCode: resp.PDU[0] &^ 0x80, ExceptionCode: code}
	}
	return resp, nil
}

// ReadHoldingRegisters performs FC03. txnID 0 means "auto-assign".
func (c *Client) ReadHoldingRegisters(txnID uint16, unitID byte, start, qty uint16) ([]uint16, protocol.Frame, error) {
	if txnID == 0 {
		txnID = c.allocTxn()
	}
	pdu := protocol.ReadHoldingRegistersRequest{StartAddress: start, Quantity: qty}.Encode()
	resp, err := c.RoundTrip(txnID, unitID, pdu)
	if err != nil {
		return nil, resp, err
	}
	values, err := protocol.DecodeReadHoldingRegistersResponse(resp.PDU)
	if err != nil {
		return nil, resp, fmt.Errorf("modbus: decode FC03 response: %w", err)
	}
	return values, resp, nil
}

// WriteMultipleRegisters performs FC10. txnID 0 means "auto-assign".
func (c *Client) WriteMultipleRegisters(txnID uint16, unitID byte, start uint16, values []uint16) (echoStart, echoQty uint16, frame protocol.Frame, err error) {
	if txnID == 0 {
		txnID = c.allocTxn()
	}
	pdu := protocol.WriteMultipleRegistersRequest{StartAddress: start, Values: values}.Encode()
	resp, err := c.RoundTrip(txnID, unitID, pdu)
	if err != nil {
		return 0, 0, resp, err
	}
	if len(resp.PDU) != 5 || resp.PDU[0] != protocol.FuncWriteMultipleRegisters {
		return 0, 0, resp, fmt.Errorf("modbus: malformed FC10 response: % X", resp.PDU)
	}
	echoStart = binary.BigEndian.Uint16(resp.PDU[1:3])
	echoQty = binary.BigEndian.Uint16(resp.PDU[3:5])
	return echoStart, echoQty, resp, nil
}

func (c *Client) allocTxn() uint16 {
	c.next++
	if c.next == 0 { // 0 is reserved-ish for auto in our CLI; skip it
		c.next = 1
	}
	return c.next
}

// SendRawBytes writes arbitrary bytes without reading a response; tests and
// the "fragment" replay directive use it to simulate 粘包/分包.
func (c *Client) SendRawBytes(b []byte) error {
	c.mu.Lock()
	defer c.mu.Unlock()
	_, err := c.conn.Write(b)
	return err
}

// ReadOneFrame reads exactly one ADU from the connection.
func (c *Client) ReadOneFrame() (protocol.Frame, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	f, err := protocol.ReadFrame(c.conn)
	if errors.Is(err, io.EOF) {
		return f, err
	}
	return f, err
}
