\set ON_ERROR_STOP on

-- Development credential-side services run as lf-dev. They may persist and
-- verify a card action, but only lf_target_dev may mutate workflow domain
-- state or create the frozen instance.
BEGIN;
REVOKE ALL PRIVILEGES ON TABLE public.workflow_role_binding_actions
    FROM "lf-dev";
GRANT SELECT, INSERT, UPDATE ON TABLE public.workflow_role_binding_actions
    TO "lf-dev";
COMMIT;
