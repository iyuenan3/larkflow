ALTER TABLE workflow_im_commands
    ADD COLUMN card_update_token text;

ALTER TABLE workflow_im_commands
    ADD CONSTRAINT workflow_im_commands_card_update_token_nonempty
    CHECK (card_update_token IS NULL OR length(card_update_token) > 0);
