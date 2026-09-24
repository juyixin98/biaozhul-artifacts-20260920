// Package server implements the Modbus TCP server: accept loop, per-
// connection MBAP framing, FC03/FC10 dispatch with exception responses,
// unit-id validation and atomic, receipt-logged writes.
package server

import (
	"context"
	"database/sql"
	"errors"
	"fmt"
	"io"
	"log"
	"net"
	"sync/atomic"
	"time"

	"modbus-receipt/internal/protocol"
	"modbus-receipt/internal/receipt"
	"modbus-receipt/internal/register"
)

// Config configures the server.
type Config struct {
	Listen       string
	Registers    int           // number of holding registers (addresses 0..N-1)
	AllowedUnits []byte        // unit ids accepted; others get exception 0x0B
	IdleTimeout  time.Duration // close a connection silent for this long
}

// Server is a Modbus TCP server.
type Server struct {
	cfg      Config
	store    *register.Store
	receipts *receipt.Store
	log      *log.Logger

	listener net.Listener
	closed   atomic.Bool
	conns    atomic.Int64
	writes   atomic.Int64
	reads    atomic.Int64
}

// New creates a server.
func New(cfg Config, store *register.Store, receipts *receipt.Store, logger *log.Logger) (*Server, error) {
	if cfg.Registers == 0 {
		cfg.Registers = store.Size()
	}
	if cfg.Registers != store.Size() {
		return nil, fmt.Errorf("register count mismatch: cfg=%d store=%d", cfg.Registers, store.Size())
	}
	if len(cfg.AllowedUnits) == 0 {
		cfg.AllowedUnits = []byte{1}
	}
	if cfg.IdleTimeout == 0 {
		cfg.IdleTimeout = 2 * time.Minute
	}
	if logger == nil {
		logger = log.Default()
	}
	return &Server{cfg: cfg, store: store, receipts: receipts, log: logger}, nil
}

// Listen starts listening.
func (s *Server) Listen() error {
	ln, err := net.Listen("tcp", s.cfg.Listen)
	if err != nil {
		return err
	}
	s.listener = ln
	s.log.Printf("modbus-tcp server listening on %s (%d holding registers, unit ids %v)",
		ln.Addr(), s.cfg.Registers, s.cfg.AllowedUnits)
	return nil
}

// Addr reports the bound listen address (useful with :0 in tests).
func (s *Server) Addr() net.Addr { return s.listener.Addr() }

// Serve accepts connections until Shutdown/Close. It always closes the
// listener; per-connection errors are logged, not returned.
func (s *Server) Serve(ctx context.Context) error {
	go func() {
		<-ctx.Done()
		s.closed.Store(true)
		s.listener.Close()
	}()

	for {
		conn, err := s.listener.Accept()
		if err != nil {
			if s.closed.Load() || errors.Is(err, net.ErrClosed) {
				return nil
			}
			return err
		}
		s.conns.Add(1)
		go s.handleConn(ctx, conn)
	}
}

// Stats returns cumulative counters since start.
func (s *Server) Stats() (connections, reads, writes int64) {
	return s.conns.Load(), s.reads.Load(), s.writes.Load()
}

func (s *Server) unitAllowed(unit byte) bool {
	for _, u := range s.cfg.AllowedUnits {
		if u == unit {
			return true
		}
	}
	return false
}

func (s *Server) handleConn(ctx context.Context, conn net.Conn) {
	defer func() {
		conn.Close()
		s.conns.Add(-1)
	}()
	remote := conn.RemoteAddr().String()

	// Close promptly on shutdown.
	go func() {
		<-ctx.Done()
		conn.SetDeadline(time.Now())
	}()

	for {
		if s.cfg.IdleTimeout > 0 {
			conn.SetReadDeadline(time.Now().Add(s.cfg.IdleTimeout))
		}
		adu, err := protocol.ReadADU(conn)
		if err != nil {
			switch {
			case errors.Is(err, io.EOF):
				return // clean half-close / close between requests
			case errors.Is(err, io.ErrUnexpectedEOF):
				s.log.Printf("%s: partial frame before disconnect (dropped per spec): %v", remote, err)
				return
			case errors.Is(context.Canceled, ctx.Err()):
				return
			default:
				var fe *protocol.FrameError
				if errors.As(err, &fe) {
					// Bad length/protocol id: no trustworthy transaction id,
					// so no exception response is possible — drop connection.
					s.log.Printf("%s: %s — closing connection", remote, fe)
				} else if isTimeout(err) && ctx.Err() != nil {
					return
				} else {
					s.log.Printf("%s: read error: %v", remote, err)
				}
				return
			}
		}

		respPDU := s.dispatch(adu)
		resp := &protocol.ADU{
			TransactionID: adu.TransactionID, // echoed verbatim, even duplicates
			ProtocolID:    protocol.ProtocolIDTCP,
			UnitID:        adu.UnitID,
			PDU:           respPDU,
		}
		wire, err := resp.Marshal()
		if err != nil {
			s.log.Printf("%s: marshal: %v", remote, err)
			return
		}
		// One Write writes the whole ADU atomically (net.TCPConn writes are
		// dispatched in one segment on the hot path); even if TCP segments
		// it, the length-driven reader reassembles it.
		if _, err := conn.Write(wire); err != nil {
			s.log.Printf("%s: write response: %v", remote, err)
			return
		}
	}
}

func isTimeout(err error) bool {
	var ne net.Error
	return errors.As(err, &ne) && ne.Timeout()
}

// dispatch validates unit id and request semantics and returns the PDU to
// send back (normal response or exception).
func (s *Server) dispatch(adu *protocol.ADU) []byte {
	txn, unit := adu.TransactionID, adu.UnitID

	if !s.unitAllowed(unit) {
		// Unknown unit: MODBUS TCP convention for an unroutable target is
		// exception 0x0B (gateway target failed to respond).
		fc := byte(0)
		if len(adu.PDU) >= 1 {
			fc = adu.PDU[0]
		}
		s.audit(txn, unit, fc, protocol.ExcGatewayTargetDeviceFailedToRespond,
			fmt.Sprintf("unit id %d not served by this device", unit))
		return protocol.ExceptionPDU(fc, protocol.ExcGatewayTargetDeviceFailedToRespond)
	}

	req, err := protocol.DecodeRequest(adu.PDU)
	if err != nil {
		var re *protocol.RequestError
		if errors.As(err, &re) {
			s.audit(txn, unit, re.Function, re.Code, re.Reason)
			return protocol.ExceptionPDU(re.Function, re.Code)
		}
		// Should not happen (framing guarantees PDU length), but if it does,
		// fail loudly rather than accepting a malformed write.
		fc := byte(0)
		if len(adu.PDU) >= 1 {
			fc = adu.PDU[0]
		}
		s.audit(txn, unit, fc, protocol.ExcSlaveDeviceFailure, err.Error())
		return protocol.ExceptionPDU(fc, protocol.ExcSlaveDeviceFailure)
	}

	switch req.Function {
	case protocol.FCReadHoldingRegisters:
		return s.handleRead(txn, unit, req)
	case protocol.FCWriteMultipleRegisters:
		return s.handleWrite(txn, unit, req)
	default:
		s.audit(txn, unit, req.Function, protocol.ExcIllegalFunction, "unsupported function")
		return protocol.ExceptionPDU(req.Function, protocol.ExcIllegalFunction)
	}
}

func (s *Server) handleRead(txn uint16, unit byte, req *protocol.Request) []byte {
	values, ok := s.store.Read(req.Address, req.Quantity)
	if !ok {
		reason := fmt.Sprintf("read %d registers from %d crosses bank of %d",
			req.Quantity, req.Address, s.store.Size())
		s.audit(txn, unit, protocol.FCReadHoldingRegisters, protocol.ExcIllegalDataAddress, reason)
		return protocol.ExceptionPDU(protocol.FCReadHoldingRegisters, protocol.ExcIllegalDataAddress)
	}
	s.reads.Add(1)
	return protocol.ReadResponsePDU(values)
}

func (s *Server) handleWrite(txn uint16, unit byte, req *protocol.Request) []byte {
	// register.WriteBatch validates the whole range before mutation, and
	// runs the commit callback while holding the exclusive lock. The
	// callback performs the SQLite transaction that durably records the
	// receipt; on failure the register image is rolled back before any
	// reader can observe it. Lock order is fixed across the program
	// (register -> receipt mutex), so no deadlock is possible.
	var rec receipt.Receipt
	err := s.store.WriteBatch(req.Address, req.Values, func() error {
		return s.receipts.WithWriteTx(func(tx *sql.Tx) error {
			var e error
			rec, e = s.receipts.RecordWrite(tx, txn, unit, req.Address, req.Values)
			return e
		})
	})
	if err != nil {
		if errors.Is(err, register.ErrOutOfRange) {
			// ANY register out of range => the ENTIRE write fails.
			reason := fmt.Sprintf("write %d registers from %d crosses bank of %d; whole batch rejected",
				req.Quantity, req.Address, s.store.Size())
			s.audit(txn, unit, protocol.FCWriteMultipleRegisters, protocol.ExcIllegalDataAddress, reason)
			return protocol.ExceptionPDU(protocol.FCWriteMultipleRegisters, protocol.ExcIllegalDataAddress)
		}
		s.log.Printf("txn=%d durable write failed, registers rolled back: %v", txn, err)
		s.audit(txn, unit, protocol.FCWriteMultipleRegisters, protocol.ExcSlaveDeviceFailure,
			"durable receipt commit failed: "+err.Error())
		return protocol.ExceptionPDU(protocol.FCWriteMultipleRegisters, protocol.ExcSlaveDeviceFailure)
	}
	s.writes.Add(1)
	s.log.Printf("write ok txn=%d unit=%d addr=%d qty=%d receipt_seq=%d chain=%s",
		txn, unit, req.Address, req.Quantity, rec.Seq, rec.ChainSHA[:16])
	return protocol.WriteResponsePDU(req.Address, req.Quantity)
}

// audit records an exception response in the durable audit log. Audit
// failures are logged but never change the protocol response.
func (s *Server) audit(txn uint16, unit, fc, code byte, reason string) {
	if err := s.receipts.RecordException(txn, unit, fc, code, reason); err != nil {
		s.log.Printf("warning: failed to persist exception audit txn=%d: %v", txn, err)
	}
}
