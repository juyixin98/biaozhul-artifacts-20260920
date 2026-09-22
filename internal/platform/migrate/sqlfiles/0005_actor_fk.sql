-- DAMS schema: actors on alerts are API keys, not human users. The original
-- 0003 migration pointed these foreign keys at users; repoint them.
ALTER TABLE alerts
    DROP CONSTRAINT IF EXISTS alerts_resolved_by_fkey,
    ADD CONSTRAINT alerts_resolved_by_fkey FOREIGN KEY (resolved_by) REFERENCES api_keys(id);

ALTER TABLE alert_status_history
    DROP CONSTRAINT IF EXISTS alert_status_history_acted_by_fkey,
    ADD CONSTRAINT alert_status_history_acted_by_fkey FOREIGN KEY (acted_by) REFERENCES api_keys(id);
