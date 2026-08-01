ALTER TABLE workflow_inbox_events
    DROP CONSTRAINT workflow_inbox_events_status_check;

ALTER TABLE workflow_inbox_events
    ADD CONSTRAINT workflow_inbox_events_status_check CHECK (
        status IN (
            'pending', 'verifying', 'verified',
            'processing', 'processed', 'failed'
        )
    ),
    ADD COLUMN verified_payload jsonb,
    ADD COLUMN failure_stage text CHECK (
        failure_stage IS NULL OR failure_stage IN ('verification', 'processing')
    );

DROP INDEX workflow_inbox_claimable_idx;

CREATE INDEX workflow_inbox_claimable_idx
    ON workflow_inbox_events (tenant_id, available_at, received_at)
    WHERE status IN ('pending', 'verifying', 'verified', 'processing', 'failed');
