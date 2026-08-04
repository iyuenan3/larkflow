CREATE UNIQUE INDEX workflow_role_binding_action_message_idx
    ON workflow_role_binding_actions (tenant_id, message_id);
