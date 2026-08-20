ALTER TABLE workflow_console_draft_requests
    DROP CONSTRAINT workflow_console_draft_requests_status_check;

ALTER TABLE workflow_console_draft_requests
    ADD COLUMN public_error_code text,
    ADD COLUMN public_error_fields jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN canceled_at timestamptz,
    ADD COLUMN canceled_by_person_id text,
    ADD CONSTRAINT workflow_console_draft_requests_status_check CHECK (
        status IN (
            'collecting', 'pending', 'generating', 'repairing', 'creating',
            'failed', 'ready', 'rejected', 'exhausted', 'canceled'
        )
    ),
    ADD CONSTRAINT workflow_console_draft_public_error_code_shape CHECK (
        public_error_code IS NULL
        OR public_error_code ~ '^[a-z][a-z0-9_]{0,63}$'
    ),
    ADD CONSTRAINT workflow_console_draft_public_error_fields_array CHECK (
        jsonb_typeof(public_error_fields) = 'array'
        AND jsonb_array_length(public_error_fields) <= 16
    ),
    ADD CONSTRAINT workflow_console_draft_cancellation_shape CHECK (
        (
            status = 'canceled'
            AND canceled_at IS NOT NULL
            AND canceled_by_person_id IS NOT NULL
        ) OR (
            status <> 'canceled'
            AND canceled_at IS NULL
            AND canceled_by_person_id IS NULL
        )
    );

CREATE INDEX workflow_console_draft_canceled_owner_idx
    ON workflow_console_draft_requests (
        tenant_id, requester_person_id, canceled_at DESC, id
    )
    WHERE status = 'canceled';
