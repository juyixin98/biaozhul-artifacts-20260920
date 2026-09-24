package gang

import "time"

// NodeView is the JSON projection of a Node.
type NodeView struct {
	ID           string            `json:"id"`
	Zone         string            `json:"zone"`
	Labels       map[string]string `json:"labels"`
	Capacity     int               `json:"capacity"`
	Online       bool              `json:"online"`
	RunningSlots int               `json:"running_slots"`
	HeldSlots    int               `json:"held_slots"`
	RunningBy    []string          `json:"running_by"`
	HeldBy       []string          `json:"held_by"`
	Version      int64             `json:"version"`
}

// PlanView is the JSON projection of a Plan.
type PlanView struct {
	ID           string   `json:"id"`
	GangID       string   `json:"gang_id"`
	Nodes        []string `json:"nodes"`
	Version      int64    `json:"version"`
	Status       string   `json:"status"`
	TTLRemaining int64    `json:"ttl_remaining_ms"`
	CommittedAt  int64    `json:"committed_at,omitempty"`
}

// GangView is the JSON projection of a Gang.
type GangView struct {
	ID           string     `json:"id"`
	MinNodes     int        `json:"min_nodes"`
	Tasks        []TaskSpec `json:"tasks"`
	AntiAffinity string     `json:"anti_affinity,omitempty"`
	TTLMillis    int        `json:"ttl_millis,omitempty"`
	Status       string     `json:"status"`
	ActivePlanID string     `json:"active_plan_id,omitempty"`
}

// StateView is the full cluster snapshot returned by GET /state.
type StateView struct {
	Generation int64      `json:"generation"`
	Nodes      []NodeView `json:"nodes"`
	Gangs      []GangView `json:"gangs"`
	Plans      []PlanView `json:"plans"`
	Queue      []string   `json:"waiting_queue"`
}

func (s *Scheduler) nodeView(n *Node, now int64) NodeView {
	running := append([]string(nil), n.runningBy...)
	held := append([]string(nil), n.reservedBy...)
	return NodeView{
		ID: n.ID, Zone: n.Zone, Labels: cloneLabels(n.Labels),
		Capacity: n.Capacity, Online: n.Online,
		RunningSlots: len(n.runningBy), HeldSlots: len(n.reservedBy),
		RunningBy: running, HeldBy: held, Version: n.version,
	}
}

func (s *Scheduler) planView(p *Plan, now int64) PlanView {
	ttl := int64(0)
	if p.Status == planReserved {
		ttl = p.expiresAt - now
		if ttl < 0 {
			ttl = 0
		}
	}
	return PlanView{
		ID: p.ID, GangID: p.GangID, Nodes: append([]string(nil), p.Nodes...),
		Version: p.Version, Status: string(p.Status),
		TTLRemaining: ttl, CommittedAt: p.committedAt,
	}
}

func gangView(g *Gang) GangView {
	return GangView{
		ID: g.Spec.ID, MinNodes: g.Spec.MinNodes,
		Tasks:        append([]TaskSpec(nil), g.Spec.Tasks...),
		AntiAffinity: g.Spec.AntiAffinity, TTLMillis: g.Spec.TTLMillis,
		Status: string(g.Status), ActivePlanID: g.ActivePlanID,
	}
}

// GetState returns a consistent snapshot of the whole scheduler.
func (s *Scheduler) GetState() StateView {
	s.mu.Lock()
	defer s.mu.Unlock()
	now := s.now()
	st := StateView{Generation: s.generation, Queue: append([]string{}, s.queued...)}
	ids := make([]string, 0, len(s.nodes))
	for id := range s.nodes {
		ids = append(ids, id)
	}
	sortStrings(ids)
	for _, id := range ids {
		st.Nodes = append(st.Nodes, s.nodeView(s.nodes[id], now))
	}
	gids := make([]string, 0, len(s.gangs))
	for id := range s.gangs {
		gids = append(gids, id)
	}
	sortStrings(gids)
	for _, id := range gids {
		st.Gangs = append(st.Gangs, gangView(s.gangs[id]))
	}
	pids := make([]string, 0, len(s.plans))
	for id := range s.plans {
		pids = append(pids, id)
	}
	sortStrings(pids)
	for _, id := range pids {
		st.Plans = append(st.Plans, s.planView(s.plans[id], now))
	}
	if st.Nodes == nil {
		st.Nodes = []NodeView{}
	}
	if st.Gangs == nil {
		st.Gangs = []GangView{}
	}
	if st.Plans == nil {
		st.Plans = []PlanView{}
	}
	return st
}

// GetGang returns one gang's projection and its active plan projection (nil
// when the gang has no active plan).
func (s *Scheduler) GetGang(id string) (GangView, *PlanView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	g, ok := s.gangs[id]
	if !ok {
		return GangView{}, nil, ErrNotFound
	}
	var pv *PlanView
	if g.ActivePlanID != "" {
		v := s.planView(s.plans[g.ActivePlanID], s.now())
		pv = &v
	}
	return gangView(g), pv, nil
}

// GetNode returns one node's projection.
func (s *Scheduler) GetNode(id string) (NodeView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	n, ok := s.nodes[id]
	if !ok {
		return NodeView{}, ErrNotFound
	}
	return s.nodeView(n, s.now()), nil
}

// GetPlan returns one plan's projection.
func (s *Scheduler) GetPlan(gangID, planID string) (PlanView, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	_, p, err := s.lookupPlan(gangID, planID)
	if err != nil {
		return PlanView{}, err
	}
	return s.planView(p, s.now()), nil
}

// DefaultTTL reports the configured default reservation TTL.
func (s *Scheduler) DefaultTTL() time.Duration {
	s.mu.Lock()
	defer s.mu.Unlock()
	return time.Duration(s.defaultTTL) * time.Millisecond
}

// sortStrings avoids an extra import churn in the view builders.
func sortStrings(s []string) {
	for i := 1; i < len(s); i++ {
		for j := i; j > 0 && s[j-1] > s[j]; j-- {
			s[j-1], s[j] = s[j], s[j-1]
		}
	}
}
