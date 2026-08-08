ALTER TABLE workflow_outbox_events
    DROP CONSTRAINT workflow_outbox_events_status_check;

ALTER TABLE workflow_outbox_events
    ADD COLUMN exhausted_at timestamptz;

ALTER TABLE workflow_outbox_events
    ADD CONSTRAINT workflow_outbox_events_status_check CHECK (
        status IN ('pending', 'processing', 'published', 'failed', 'exhausted')
    );

ALTER TABLE workflow_outbox_events
    ADD CONSTRAINT workflow_outbox_events_exhaustion_shape CHECK (
        (status = 'exhausted' AND exhausted_at IS NOT NULL)
        OR (status <> 'exhausted' AND exhausted_at IS NULL)
    );
