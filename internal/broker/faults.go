package broker

import "sync"

func ptr[T any](v T) *T { return &v }

// Faults holds the two simulated failure sets:
//
//	fail:    business transaction is forced to ROLL BACK (commit failure).
//	         Nothing is durable; the redelivery will INSERT the event.
//	dropAck: the transaction COMMITS but the PUBACK is deliberately dropped
//	         (simulating a lost ACK / crashed process right after commit).
//	         The redelivery hits the business dedup key: one business row,
//	         kind='dup' raw audit row.
//
// dropAck entries are ONE-SHOT: they are consumed on first check, so the
// redelivery itself is acknowledged and the pipeline self-heals.
//
// The sentinal device id "*" on FailHook means "any commit failure is armed"
// and gates the reconnect backoff loop.
type Faults struct {
	mu      sync.Mutex
	fail    map[string]struct{}
	dropAck map[string]struct{}
}

// NewFaults creates empty fault sets.
func NewFaults() *Faults {
	return &Faults{
		fail:    make(map[string]struct{}),
		dropAck: make(map[string]struct{}),
	}
}

// FailHook reports commit-failure state; "*" asks whether any is armed.
func (f *Faults) FailHook(deviceID string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if deviceID == "*" {
		return len(f.fail) > 0
	}
	_, ok := f.fail[deviceID]
	return ok
}

// Arm forces business transaction rollback for a device until cleared.
func (f *Faults) Arm(deviceID string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.fail[deviceID] = struct{}{}
}

// Clear removes the commit failure for a device ("*" clears all).
func (f *Faults) Clear(deviceID string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if deviceID == "*" {
		f.fail = make(map[string]struct{})
		return
	}
	delete(f.fail, deviceID)
}

// ArmDropAck arms a one-shot "commit succeeds, ACK lost" for a device.
func (f *Faults) ArmDropAck(deviceID string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.dropAck[deviceID] = struct{}{}
}

// ConsumeDropAck reports and atomically consumes the one-shot drop-ACK token.
func (f *Faults) ConsumeDropAck(deviceID string) bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	if _, ok := f.dropAck[deviceID]; ok {
		delete(f.dropAck, deviceID)
		return true
	}
	return false
}

// List returns the currently armed devices of both kinds.
func (f *Faults) List() (fail []string, dropAck []string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for d := range f.fail {
		fail = append(fail, d)
	}
	for d := range f.dropAck {
		dropAck = append(dropAck, d)
	}
	return fail, dropAck
}
