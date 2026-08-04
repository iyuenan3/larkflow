ALTER TABLE workflow_role_binding_actions
    ADD COLUMN is_canonical boolean NOT NULL DEFAULT true;

WITH ranked AS (
    SELECT tenant_id, id,
           row_number() OVER (
               PARTITION BY tenant_id, message_id
               ORDER BY received_at, id
           ) AS action_rank
    FROM workflow_role_binding_actions
)
UPDATE workflow_role_binding_actions AS action
SET is_canonical = false
FROM ranked
WHERE action.tenant_id = ranked.tenant_id
  AND action.id = ranked.id
  AND ranked.action_rank > 1;

CREATE UNIQUE INDEX workflow_role_binding_action_message_idx
    ON workflow_role_binding_actions (tenant_id, message_id)
    WHERE is_canonical;
