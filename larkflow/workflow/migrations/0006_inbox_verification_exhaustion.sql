ALTER TABLE workflow_inbox_events
    DROP CONSTRAINT workflow_inbox_events_status_check;

ALTER TABLE workflow_inbox_events
    ADD CONSTRAINT workflow_inbox_events_status_check CHECK (
        status IN (
            'pending', 'verifying', 'verified',
            'processing', 'processed', 'failed', 'exhausted'
        )
    );
