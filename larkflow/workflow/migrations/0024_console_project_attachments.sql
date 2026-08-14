ALTER TABLE workflow_console_draft_requests
    DROP CONSTRAINT workflow_console_draft_requests_status_check;

ALTER TABLE workflow_console_draft_requests
    ADD CONSTRAINT workflow_console_draft_requests_status_check CHECK (
        status IN (
            'collecting', 'pending', 'generating', 'repairing', 'creating',
            'failed', 'ready', 'rejected', 'exhausted'
        )
    ),
    ADD COLUMN generation_deferred boolean NOT NULL DEFAULT false,
    ADD COLUMN attachment_manifest jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD CONSTRAINT workflow_console_draft_attachment_manifest_array CHECK (
        jsonb_typeof(attachment_manifest) = 'array'
    ),
    ADD CONSTRAINT workflow_console_draft_collecting_deferred CHECK (
        status <> 'collecting' OR generation_deferred
    );

CREATE TABLE workflow_project_attachments (
    tenant_id text NOT NULL,
    attachment_id text NOT NULL CHECK (
        attachment_id ~ '^[0-9a-f]{32}$'
    ),
    origin_request_id text NOT NULL,
    instance_id text,
    uploader_person_id text NOT NULL,
    display_filename text NOT NULL CHECK (
        char_length(display_filename) BETWEEN 1 AND 120
        AND position('/' IN display_filename) = 0
        AND position(chr(92) IN display_filename) = 0
    ),
    media_type text NOT NULL CHECK (
        media_type IN ('text/plain', 'text/markdown')
    ),
    size_bytes integer NOT NULL CHECK (
        size_bytes BETWEEN 1 AND 32768
    ),
    content_sha256 text NOT NULL CHECK (
        content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    object_key text NOT NULL CHECK (
        object_key ~ '^[0-9a-f]{16}/[0-9a-f]{32}$'
    ),
    status text NOT NULL CHECK (status IN ('ready', 'revoked')),
    data_classification text NOT NULL CHECK (
        data_classification = 'internal'
    ),
    model_egress_policy text NOT NULL CHECK (
        model_egress_policy IN ('allow', 'deny')
    ),
    created_at timestamptz NOT NULL,
    revoked_at timestamptz,
    PRIMARY KEY (tenant_id, attachment_id),
    UNIQUE (tenant_id, object_key),
    FOREIGN KEY (tenant_id, origin_request_id)
        REFERENCES workflow_console_draft_requests (tenant_id, id),
    FOREIGN KEY (tenant_id, instance_id)
        REFERENCES workflow_instances (tenant_id, id),
    CONSTRAINT workflow_project_attachment_revocation_shape CHECK (
        (status = 'ready' AND revoked_at IS NULL)
        OR (status = 'revoked' AND revoked_at IS NOT NULL)
    )
);

CREATE INDEX workflow_project_attachment_request_idx
    ON workflow_project_attachments (
        tenant_id, origin_request_id, status, created_at, attachment_id
    );

CREATE INDEX workflow_project_attachment_instance_idx
    ON workflow_project_attachments (tenant_id, instance_id, attachment_id)
    WHERE instance_id IS NOT NULL;

CREATE FUNCTION workflow_project_attachments_deny_delete()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'workflow project attachments are append-preserved';
END;
$$;

CREATE TRIGGER workflow_project_attachments_preserve_history
    BEFORE DELETE ON workflow_project_attachments
    FOR EACH ROW
    EXECUTE FUNCTION workflow_project_attachments_deny_delete();
