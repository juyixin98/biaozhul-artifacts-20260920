package broker

// CloseAllConns forcibly closes every live MQTT connection. Used during
// shutdown. Will messages are suppressed (see Conn.adminShutdown).
func (b *Broker) CloseAllConns() {
	b.mu.Lock()
	var conns []*Conn
	for _, s := range b.sessions {
		if s.conn != nil {
			conns = append(conns, s.conn)
		}
	}
	b.mu.Unlock()
	for _, c := range conns {
		c.adminDeleted = true // reuse the "suppress will" path
		c.stop()
		_ = c.nc.Close()
	}
}
