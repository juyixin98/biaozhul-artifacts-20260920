package sim

// nodeHandle is the node-side message switch. Handlers are invoked by the
// event loop when a message copy is delivered.
func (s *Sim) nodeHandle(n *Node, _ string, v any) {
	switch m := v.(type) {
	case msgClientPut:
		n.putIfNewer(m.Key, Record{Value: m.Value, Version: m.Version})
		s.send(n.id, routerAddr, message{msgPutAck{ReqID: m.ReqID, Gen: m.Gen, NodeID: n.id}})

	case msgClientGet:
		rec, ok := n.store[m.Key]
		s.send(n.id, routerAddr, message{msgGetResp{
			ReqID: m.ReqID, Gen: m.Gen, NodeID: n.id, Rec: rec, Found: ok,
		}})

	case msgFetch:
		page, next, done := n.scanRange(s.router.taskRange(m.TaskID), m.Cursor, m.Limit)
		s.send(n.id, routerAddr, message{msgBatch{
			TaskID: m.TaskID, Gen: m.Gen, Records: page, NextCursor: next, Done: done,
		}})

	case msgTransferPut:
		cnt := 0
		for _, p := range m.Records {
			if n.putIfNewer(p.Key, p.Rec) {
				cnt++
			}
		}
		s.send(n.id, routerAddr, message{msgTransferAck{TaskID: m.TaskID, Gen: m.Gen, Count: cnt}})

	case msgDecommission:
		n.alive = false
		s.send(n.id, routerAddr, message{msgDecommissionAck{NodeID: n.id}})
	}
}
