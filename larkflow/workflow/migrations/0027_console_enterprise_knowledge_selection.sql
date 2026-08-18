CREATE FUNCTION workflow_enterprise_source_ids_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    item jsonb;
BEGIN
    IF jsonb_typeof(value) <> 'array' OR jsonb_array_length(value) > 16 THEN
        RETURN false;
    END IF;
    FOR item IN SELECT * FROM jsonb_array_elements(value)
    LOOP
        IF jsonb_typeof(item) <> 'string'
           OR trim(both '"' from item::text) !~ '^enterprise:[a-z][a-z0-9_.:-]{0,116}$'
        THEN
            RETURN false;
        END IF;
    END LOOP;
    RETURN true;
END;
$$;

ALTER TABLE workflow_console_draft_requests
    ADD COLUMN enterprise_source_selection jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN enterprise_selection_version integer NOT NULL DEFAULT 0,
    ADD COLUMN enterprise_knowledge_manifest jsonb NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN enterprise_selection_fingerprint text,
    ADD CONSTRAINT workflow_console_draft_enterprise_selection_array CHECK (
        workflow_enterprise_source_ids_valid(enterprise_source_selection)
    ),
    ADD CONSTRAINT workflow_console_draft_enterprise_selection_version CHECK (
        enterprise_selection_version >= 0
    ),
    ADD CONSTRAINT workflow_console_draft_enterprise_manifest_array CHECK (
        jsonb_typeof(enterprise_knowledge_manifest) = 'array'
        AND jsonb_array_length(enterprise_knowledge_manifest) <= 16
    ),
    ADD CONSTRAINT workflow_console_draft_enterprise_fingerprint_shape CHECK (
        enterprise_selection_fingerprint IS NULL
        OR enterprise_selection_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT workflow_console_draft_enterprise_freeze_shape CHECK (
        (
            jsonb_array_length(enterprise_knowledge_manifest) = 0
            AND enterprise_selection_fingerprint IS NULL
        ) OR (
            jsonb_array_length(enterprise_knowledge_manifest) > 0
            AND enterprise_selection_fingerprint IS NOT NULL
        )
    );

CREATE FUNCTION workflow_console_draft_enterprise_selection_guard()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.enterprise_source_selection IS DISTINCT FROM OLD.enterprise_source_selection
       OR NEW.enterprise_selection_version IS DISTINCT FROM OLD.enterprise_selection_version
    THEN
        IF OLD.status <> 'collecting' OR NEW.status <> 'collecting' THEN
            RAISE EXCEPTION 'enterprise knowledge selection is only mutable while collecting';
        END IF;
    END IF;

    IF NEW.enterprise_knowledge_manifest IS DISTINCT FROM OLD.enterprise_knowledge_manifest
       OR NEW.enterprise_selection_fingerprint IS DISTINCT FROM OLD.enterprise_selection_fingerprint
    THEN
        IF OLD.status <> 'collecting' OR NEW.status <> 'pending' THEN
            RAISE EXCEPTION 'enterprise knowledge manifest can only be frozen when queued';
        END IF;
    END IF;

    IF jsonb_array_length(OLD.enterprise_knowledge_manifest) > 0
       AND (
           NEW.enterprise_knowledge_manifest IS DISTINCT FROM OLD.enterprise_knowledge_manifest
           OR NEW.enterprise_selection_fingerprint IS DISTINCT FROM OLD.enterprise_selection_fingerprint
       )
    THEN
        RAISE EXCEPTION 'enterprise knowledge manifest is immutable';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER workflow_console_draft_enterprise_selection_guard
    BEFORE UPDATE ON workflow_console_draft_requests
    FOR EACH ROW
    EXECUTE FUNCTION workflow_console_draft_enterprise_selection_guard();
