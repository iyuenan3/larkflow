ALTER TABLE workflow_role_binding_actions
    ADD COLUMN progress_stage text CHECK (
        progress_stage IS NULL OR progress_stage IN ('generating', 'repairing')
    ),
    ADD COLUMN progress_revision integer NOT NULL DEFAULT 0 CHECK (
        progress_revision >= 0
    ),
    ADD COLUMN progress_status text CHECK (
        progress_status IS NULL OR progress_status IN (
            'pending', 'sending', 'sent', 'failed'
        )
    ),
    ADD COLUMN progress_attempt_count integer NOT NULL DEFAULT 0 CHECK (
        progress_attempt_count >= 0
    ),
    ADD COLUMN progress_available_at timestamptz,
    ADD COLUMN progress_claimed_by text,
    ADD COLUMN progress_claim_token text,
    ADD COLUMN progress_claim_expires_at timestamptz,
    ADD COLUMN progress_claim_revision integer CHECK (
        progress_claim_revision IS NULL OR progress_claim_revision >= 1
    ),
    ADD COLUMN progress_sent_at timestamptz,
    ADD COLUMN progress_last_error text;

CREATE INDEX workflow_role_binding_progress_claimable_idx
    ON workflow_role_binding_actions (
        tenant_id, progress_available_at, received_at
    )
    WHERE progress_status IN ('pending', 'sending', 'failed');

DROP TRIGGER workflow_role_binding_worker_wakeup
    ON workflow_role_binding_actions;

CREATE TRIGGER workflow_role_binding_worker_wakeup
    AFTER INSERT OR UPDATE OF
        status, available_at, reply_status, reply_available_at,
        progress_status, progress_available_at
    ON workflow_role_binding_actions
    FOR EACH ROW
    WHEN (
        NEW.status IN ('pending', 'verified', 'failed')
        OR NEW.reply_status IN ('pending', 'failed')
        OR NEW.progress_status IN ('pending', 'failed')
    )
    EXECUTE FUNCTION workflow_notify_worker_wakeup();
