CREATE TABLE workflow_console_draft_requests (
    tenant_id text NOT NULL,
    id text NOT NULL CHECK (id ~ '^[0-9a-f]{32}$'),
    requester_person_id text NOT NULL,
    collaborator_person_id text NOT NULL,
    brief text NOT NULL CHECK (
        char_length(brief) BETWEEN 1 AND 1000
    ),
    context text NOT NULL DEFAULT '' CHECK (
        char_length(context) <= 1000
    ),
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'generating', 'repairing', 'creating', 'failed',
            'ready', 'rejected', 'exhausted'
        )
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL,
    claimed_by text,
    claim_token text,
    claim_expires_at timestamptz,
    definition jsonb,
    instance_id text,
    last_error text,
    created_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    completed_at timestamptz,
    PRIMARY KEY (tenant_id, id),
    CONSTRAINT workflow_console_draft_definition_object CHECK (
        definition IS NULL OR jsonb_typeof(definition) = 'object'
    ),
    CONSTRAINT workflow_console_draft_ready_instance CHECK (
        status <> 'ready' OR (definition IS NOT NULL AND instance_id IS NOT NULL)
    )
);

CREATE UNIQUE INDEX workflow_console_draft_instance_idx
    ON workflow_console_draft_requests (tenant_id, instance_id)
    WHERE instance_id IS NOT NULL;

CREATE INDEX workflow_console_draft_owner_recent_idx
    ON workflow_console_draft_requests (
        tenant_id, requester_person_id, created_at DESC, id DESC
    );

CREATE INDEX workflow_console_draft_claimable_idx
    ON workflow_console_draft_requests (tenant_id, available_at, created_at, id)
    WHERE status IN (
        'pending', 'generating', 'repairing', 'creating', 'failed'
    );

CREATE TRIGGER workflow_console_draft_worker_wakeup
    AFTER INSERT OR UPDATE OF status, available_at
    ON workflow_console_draft_requests
    FOR EACH ROW
    WHEN (NEW.status IN ('pending', 'creating', 'failed'))
    EXECUTE FUNCTION workflow_notify_worker_wakeup();
