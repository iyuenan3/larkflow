ALTER TABLE workflow_im_commands
    ADD COLUMN feedback_status text CHECK (
        feedback_status IS NULL OR feedback_status IN ('updated', 'failed')
    ),
    ADD COLUMN feedback_elapsed_ms integer CHECK (
        feedback_elapsed_ms IS NULL OR feedback_elapsed_ms >= 0
    ),
    ADD COLUMN feedback_completed_at timestamptz;

ALTER TABLE workflow_im_commands
    ADD CONSTRAINT workflow_im_commands_feedback_complete
    CHECK (
        (feedback_status IS NULL
         AND feedback_elapsed_ms IS NULL
         AND feedback_completed_at IS NULL)
        OR
        (feedback_status IS NOT NULL
         AND feedback_elapsed_ms IS NOT NULL
         AND feedback_completed_at IS NOT NULL)
    );

ALTER TABLE workflow_role_binding_actions
    ADD COLUMN feedback_status text CHECK (
        feedback_status IS NULL OR feedback_status IN ('updated', 'failed')
    ),
    ADD COLUMN feedback_elapsed_ms integer CHECK (
        feedback_elapsed_ms IS NULL OR feedback_elapsed_ms >= 0
    ),
    ADD COLUMN feedback_completed_at timestamptz;

ALTER TABLE workflow_role_binding_actions
    ADD CONSTRAINT workflow_role_binding_actions_feedback_complete
    CHECK (
        (feedback_status IS NULL
         AND feedback_elapsed_ms IS NULL
         AND feedback_completed_at IS NULL)
        OR
        (feedback_status IS NOT NULL
         AND feedback_elapsed_ms IS NOT NULL
         AND feedback_completed_at IS NOT NULL)
    );
