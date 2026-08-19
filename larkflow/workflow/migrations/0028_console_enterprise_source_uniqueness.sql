CREATE OR REPLACE FUNCTION workflow_enterprise_source_ids_valid(value jsonb)
RETURNS boolean
LANGUAGE plpgsql
IMMUTABLE
AS $$
DECLARE
    item jsonb;
    source_count integer;
    distinct_source_count integer;
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
    SELECT COUNT(*), COUNT(DISTINCT source_id)
    INTO source_count, distinct_source_count
    FROM jsonb_array_elements_text(value) AS source_ids(source_id);
    RETURN source_count = distinct_source_count;
END;
$$;

ALTER TABLE workflow_console_draft_requests
    DROP CONSTRAINT workflow_console_draft_enterprise_selection_array;

ALTER TABLE workflow_console_draft_requests
    ADD CONSTRAINT workflow_console_draft_enterprise_selection_array CHECK (
        workflow_enterprise_source_ids_valid(enterprise_source_selection)
    );
