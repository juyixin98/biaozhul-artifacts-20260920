// Package register holds the Modbus holding-register image and enforces
// atomic batch semantics: concurrent readers never observe a partially
// applied Write Multiple Registers request.
package register

import (
	"fmt"
	"sync"
)

// Store is a fixed-size bank of 16-bit holding registers (addresses 0..N-1).
type Store struct {
	mu  sync.RWMutex
	reg []uint16
}

// New creates a register bank with nRegisters holding registers.
func New(nRegisters int) *Store {
	if nRegisters <= 0 || nRegisters > 65536 {
		panic(fmt.Sprintf("register count %d out of range", nRegisters))
	}
	return &Store{reg: make([]uint16, nRegisters)}
}

// Size returns the number of holding registers.
func (s *Store) Size() int { return len(s.reg) }

// Read returns a copy of qty registers starting at addr. It returns
// false when the range crosses the register bank boundary; the caller
// answers with ILLEGAL DATA ADDRESS and no state is exposed.
//
// A single RLock spans the copy, so the snapshot can never straddle a
// concurrent WriteBatch commit.
func (s *Store) Read(addr, qty uint16) ([]uint16, bool) {
	if qty == 0 || int(addr)+int(qty) > len(s.reg) {
		return nil, false
	}
	s.mu.RLock()
	out := make([]uint16, qty)
	copy(out, s.reg[addr:int(addr)+int(qty)])
	s.mu.RUnlock()
	return out, true
}

// WriteBatch validates and applies one FC10 write as a single atomic step.
//
// Validation first, mutation second: if ANY register in [addr, addr+qty)
// lies outside the bank, the entire write fails (returns false) and the
// register image is untouched.
//
// The optional commit callback runs while the write lock is held and the
// new values are already in place. If commit returns an error, the values
// are rolled back before the lock is released, so register state and the
// durable receipt log cannot diverge. The callback must not call back
// into the store.
func (s *Store) WriteBatch(addr uint16, values []uint16, commit func() error) error {
	qty := uint16(len(values))
	if qty == 0 || int(addr)+int(qty) > len(s.reg) {
		return ErrOutOfRange
	}

	s.mu.Lock()
	defer s.mu.Unlock()

	// Snapshot the overwritten words so a failed commit can roll back.
	old := make([]uint16, qty)
	copy(old, s.reg[addr:addr+qty])
	copy(s.reg[addr:addr+qty], values)

	if commit != nil {
		if err := commit(); err != nil {
			copy(s.reg[addr:addr+qty], old)
			return err
		}
	}
	return nil
}

// RangeError signals that an access crosses the register bank boundary.
type RangeError struct{}

func (RangeError) Error() string { return "register access out of range" }

// ErrOutOfRange is returned by WriteBatch for any partially/fully
// out-of-range write; the whole batch is rejected.
var ErrOutOfRange = RangeError{}
