ALTER TABLE workflow_im_commands
    ADD COLUMN reply_kind text NOT NULL DEFAULT 'text' CHECK (
        reply_kind IN ('text', 'role_binding_card')
    ),
    ADD COLUMN role_binding_request jsonb,
    ADD COLUMN role_binding_candidates jsonb;

ALTER TABLE workflow_im_commands
    ADD CONSTRAINT workflow_im_commands_role_binding_request_object
    CHECK (
        role_binding_request IS NULL
        OR jsonb_typeof(role_binding_request) = 'object'
    ),
    ADD CONSTRAINT workflow_im_commands_role_binding_candidates_array
    CHECK (
        role_binding_candidates IS NULL
        OR jsonb_typeof(role_binding_candidates) = 'array'
    );

CREATE INDEX workflow_im_role_card_claimable_idx
    ON workflow_im_commands (tenant_id, reply_available_at, processed_at)
    WHERE reply_kind = 'role_binding_card'
      AND reply_status IN ('pending', 'sending', 'failed');

CREATE TABLE workflow_role_binding_actions (
    tenant_id text NOT NULL,
    id text NOT NULL,
    message_id text NOT NULL,
    chat_id text NOT NULL,
    operator_person_id text NOT NULL,
    action_tag text NOT NULL,
    action_name text NOT NULL,
    form_value text NOT NULL,
    update_token text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (
        status IN (
            'pending', 'verifying', 'verified', 'processing',
            'processed', 'rejected', 'failed'
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
    owner_bindings jsonb,
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
    PRIMARY KEY (tenant_id, id)
);

ALTER TABLE workflow_role_binding_actions
    ADD CONSTRAINT workflow_role_binding_actions_owner_bindings_object
    CHECK (
        owner_bindings IS NULL OR jsonb_typeof(owner_bindings) = 'object'
    );

CREATE INDEX workflow_role_binding_action_claimable_idx
    ON workflow_role_binding_actions (tenant_id, available_at, received_at)
    WHERE status IN (
        'pending', 'verifying', 'verified', 'processing', 'failed'
    );

CREATE INDEX workflow_role_binding_reply_claimable_idx
    ON workflow_role_binding_actions (
        tenant_id, reply_available_at, processed_at
    )
    WHERE reply_status IN ('pending', 'sending', 'failed');

CREATE UNIQUE INDEX workflow_im_role_card_message_idx
    ON workflow_im_commands (tenant_id, reply_external_id)
    WHERE reply_kind = 'role_binding_card' AND reply_external_id IS NOT NULL;
