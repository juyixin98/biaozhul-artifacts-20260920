package sim

import "fmt"

func keyName(idx int) string { return fmt.Sprintf("key-%04d", idx) }

// scheduleWorkload registers every client op as a timed event. Client c's op j
// targets key "key-%04d" with index j mod Keys, so clients sharing a keyspace
// write the same keys concurrently. Every ReadEvery-th op is a read of the key
// written by the previous op; the rest are writes.
func (s *Sim) scheduleWorkload() {
	for ci := range s.cfg.Clients {
		c := s.cfg.Clients[ci]
		if c.Keys <= 0 || c.Ops <= 0 {
			continue
		}
		for j := 0; j < c.Ops; j++ {
			j := j
			t := c.StartMs + int64(j)*c.IntervalMs
			s.schedule(t, func() {
				read := c.ReadEvery > 0 && j%c.ReadEvery == c.ReadEvery-1
				idx := j % c.Keys
				if read {
					idx = (j - 1) % c.Keys // read the key from the previous op
					if idx < 0 {
						idx = 0
					}
					s.router.clientGet(c.ID, keyName(idx))
				} else {
					s.router.clientWrite(c.ID, keyName(idx), fmt.Sprintf("%s#%d", c.ID, j))
				}
			})
		}
	}
}
