ALTER TABLE workflow_im_commands
    ADD COLUMN mentions jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE workflow_im_commands
    ADD CONSTRAINT workflow_im_commands_mentions_array
    CHECK (jsonb_typeof(mentions) = 'array');
