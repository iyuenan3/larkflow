CREATE INDEX workflow_instances_owner_recent_idx
    ON workflow_instances (tenant_id, owner_person_id, created_at DESC, id DESC);
