ALTER TABLE workflow_restart_previews
    ADD COLUMN scope text NOT NULL DEFAULT 'node';

ALTER TABLE workflow_restart_previews
    ALTER COLUMN node_key DROP NOT NULL;

ALTER TABLE workflow_restart_previews
    ADD CONSTRAINT workflow_restart_previews_scope_check CHECK (
        (scope = 'node' AND node_key IS NOT NULL AND btrim(node_key) <> '')
        OR (scope = 'instance' AND node_key IS NULL)
    );
