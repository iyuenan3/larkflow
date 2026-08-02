CREATE TABLE workflow_im_commands (
    tenant_id text NOT NULL,
    id text NOT NULL,
    message_id text NOT NULL,
    chat_id text NOT NULL,
    sender_person_id text NOT NULL,
    text text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'verifying', 'verified', 'processing',
            'processed', 'failed', 'exhausted'
        )
    ),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    available_at timestamptz NOT NULL,
    claimed_by text,
    claim_token text,
    claim_expires_at timestamptz,
    verified_at timestamptz,
    processed_at timestamptz,
    outcome text,
    instance_id text,
    last_error text,
    failure_stage text,
    reply_text text,
    reply_status text CHECK (
        reply_status IS NULL OR reply_status IN (
            'pending', 'sending', 'sent', 'failed'
        )
    ),
    reply_attempt_count integer NOT NULL DEFAULT 0 CHECK (
        reply_attempt_count >= 0
    ),
    reply_available_at timestamptz,
    reply_claimed_by text,
    reply_claim_token text,
    reply_claim_expires_at timestamptz,
    reply_external_id text,
    reply_sent_at timestamptz,
    reply_last_error text,
    occurred_at timestamptz NOT NULL,
    received_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, id),
    UNIQUE (tenant_id, message_id)
);

CREATE INDEX workflow_im_command_claimable_idx
    ON workflow_im_commands (tenant_id, available_at, received_at)
    WHERE status IN (
        'pending', 'verifying', 'verified', 'processing', 'failed'
    );

CREATE INDEX workflow_im_reply_claimable_idx
    ON workflow_im_commands (tenant_id, reply_available_at, processed_at)
    WHERE reply_status IN ('pending', 'sending', 'failed');
