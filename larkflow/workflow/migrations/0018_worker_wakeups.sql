CREATE FUNCTION workflow_notify_worker_wakeup() RETURNS trigger AS $$
BEGIN
    PERFORM pg_notify('larkflow_work_available', '');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER workflow_outbox_worker_wakeup
    AFTER INSERT OR UPDATE OF status, available_at
    ON workflow_outbox_events
    FOR EACH ROW
    WHEN (NEW.status IN ('pending', 'failed'))
    EXECUTE FUNCTION workflow_notify_worker_wakeup();

CREATE TRIGGER workflow_inbox_worker_wakeup
    AFTER INSERT OR UPDATE OF status, available_at
    ON workflow_inbox_events
    FOR EACH ROW
    WHEN (NEW.status IN ('pending', 'verified', 'failed'))
    EXECUTE FUNCTION workflow_notify_worker_wakeup();

CREATE TRIGGER workflow_im_command_worker_wakeup
    AFTER INSERT OR UPDATE OF status, available_at, reply_status, reply_available_at
    ON workflow_im_commands
    FOR EACH ROW
    WHEN (
        NEW.status IN ('pending', 'verified', 'failed')
        OR NEW.reply_status IN ('pending', 'failed')
    )
    EXECUTE FUNCTION workflow_notify_worker_wakeup();

CREATE TRIGGER workflow_role_binding_worker_wakeup
    AFTER INSERT OR UPDATE OF status, available_at, reply_status, reply_available_at
    ON workflow_role_binding_actions
    FOR EACH ROW
    WHEN (
        NEW.status IN ('pending', 'verified', 'failed')
        OR NEW.reply_status IN ('pending', 'failed')
    )
    EXECUTE FUNCTION workflow_notify_worker_wakeup();
