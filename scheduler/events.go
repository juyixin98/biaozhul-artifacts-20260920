package scheduler

import (
	"sync"
	"time"
)

// EventType identifies a structured state-change record.
type EventType string

const (
	EventSubmitted          EventType = "submitted"
	EventAdmitted           EventType = "admitted"
	EventRejectedInfeasible EventType = "rejected_infeasible"
	EventStarted            EventType = "started"
	EventCompleted          EventType = "completed"
	EventTimeout            EventType = "timeout" // ran past its declared bound
	EventCancelRequested    EventType = "cancel_requested"
	EventCancelled          EventType = "cancelled"
	EventDeadlineMet        EventType = "deadline_met"
	EventDeadlineMissed     EventType = "deadline_missed"
	EventSlotAcquired       EventType = "slot_acquired"
	EventSlotReleased       EventType = "slot_released"
)

// Event is one structured record of a scheduler state change.
type Event struct {
	Seq    int64             `json:"seq"`
	Time   time.Time         `json:"time"`
	JobID  string            `json:"job_id,omitempty"`
	Type   EventType         `json:"type"`
	Detail map[string]string `json:"detail,omitempty"`
}

// EventLog is a thread-safe in-memory append-only log of Events.
type EventLog struct {
	mu     sync.Mutex
	clock  Clock
	events []Event
}

func NewEventLog(clock Clock) *EventLog {
	return &EventLog{clock: clock}
}

// Record appends an event and returns its assigned sequence number.
func (l *EventLog) Record(jobID string, typ EventType, detail map[string]string) Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	e := Event{
		Seq:    int64(len(l.events)) + 1,
		Time:   l.clock.Now(),
		JobID:  jobID,
		Type:   typ,
		Detail: detail,
	}
	l.events = append(l.events, e)
	return e
}

// Events returns a copy of all recorded events in order.
func (l *EventLog) Events() []Event {
	l.mu.Lock()
	defer l.mu.Unlock()
	out := make([]Event, len(l.events))
	copy(out, l.events)
	return out
}
