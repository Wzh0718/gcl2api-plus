CREATE INDEX idx_disabled
            ON credentials(disabled);

CREATE INDEX idx_rotation_order
            ON credentials(rotation_order);

CREATE INDEX idx_ag_disabled
            ON antigravity_credentials(disabled);

CREATE INDEX idx_ag_rotation_order
            ON antigravity_credentials(rotation_order);

CREATE INDEX idx_ag_proxy_group
            ON antigravity_credentials(proxy_group_id);

CREATE INDEX idx_ag_bound_proxy_node
            ON antigravity_credentials(bound_proxy_node_id);

CREATE INDEX idx_proxy_group_nodes_group
            ON proxy_group_nodes(group_id, enabled, id);

CREATE INDEX idx_api_keys_status
            ON api_keys(status, kind, created_at);

CREATE INDEX idx_api_key_quota_events_key
            ON api_key_quota_events(api_key_id, created_at);

CREATE INDEX idx_billing_daily_date
            ON billing_daily(billing_date);

CREATE INDEX idx_billing_daily_credential
            ON billing_daily(credential_name, billing_date);

CREATE INDEX idx_billing_daily_model
            ON billing_daily(model, billing_date);

CREATE INDEX idx_billing_dedupe_expiry
            ON billing_request_dedupe(expires_at);

CREATE INDEX idx_model_stats_daily_date
            ON credential_model_stats_daily(mode, stat_date);

CREATE INDEX idx_call_records_created
            ON call_records(created_at);

CREATE INDEX idx_call_records_key_created
            ON call_records(api_key_id, created_at);

CREATE INDEX idx_call_records_cred_created
            ON call_records(credential_name, created_at);

CREATE INDEX idx_billing_daily_api_key ON billing_daily(api_key_id, billing_date);

CREATE INDEX idx_warmup_leases_account
            ON antigravity_warmup_leases(credential_name, expires_at);

CREATE INDEX idx_warmup_leases_ip
            ON antigravity_warmup_leases(ip_key, expires_at);

CREATE INDEX idx_ag_routing_turn
            ON antigravity_credentials(routing_turn);

CREATE INDEX idx_call_records_timing_id ON call_records(timing_id) WHERE timing_id IS NOT NULL;

CREATE INDEX idx_credential_audit_time ON credential_audit_events(created_at);

CREATE INDEX telemetry_events_created ON antigravity_telemetry_events(created_at);

CREATE INDEX telemetry_events_timing ON antigravity_telemetry_events(timing_id);

CREATE INDEX idx_ag_egress_reservations_ip
                            ON antigravity_egress_reservations(egress_ip, expires_at);

CREATE INDEX account_usage_bucket ON account_usage(bucket);

CREATE INDEX idx_key_recent_requests
            ON api_key_recent_requests(key_id, admitted_at);

CREATE UNIQUE INDEX idx_ag_fixed_proxy_ip
            ON antigravity_credentials(fixed_proxy_ip) WHERE fixed_proxy_ip IS NOT NULL;

CREATE INDEX idx_refresh_import_ready
                ON refresh_import_items(status, id);

CREATE INDEX idx_refresh_import_token
                ON refresh_import_items(token_digest) WHERE status = 'imported';

CREATE UNIQUE INDEX api_keys_ascii_nocase_name ON api_keys ((translate(name, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz') COLLATE "C"));
