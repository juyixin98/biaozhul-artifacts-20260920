package main

import (
	"context"
	"log"
	"time"

	"deadlockcheck/internal/service"
)

// sweeper periodically moves lease-expired running tasks into 'uncertain'.
type sweeper struct {
	svc      *service.Service
	interval time.Duration
	done     chan struct{}
}

func newSweeper(svc *service.Service, interval time.Duration) *sweeper {
	if interval <= 0 {
		interval = time.Second
	}
	return &sweeper{svc: svc, interval: interval, done: make(chan struct{})}
}

func (s *sweeper) Start(ctx context.Context) {
	go func() {
		t := time.NewTicker(s.interval)
		defer t.Stop()
		for {
			select {
			case <-ctx.Done():
				close(s.done)
				return
			case <-t.C:
				ids, err := s.svc.SweepTimeouts(ctx)
				if err != nil {
					log.Printf("sweep error: %v", err)
					continue
				}
				for _, id := range ids {
					log.Printf("task %s lease expired -> uncertain (resources fenced, not released)", id)
				}
			}
		}
	}()
}

func (s *sweeper) Stop() { <-s.done }
