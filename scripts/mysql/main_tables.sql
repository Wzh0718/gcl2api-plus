CREATE TABLE IF NOT EXISTS `credential_refresh_leases` (
  `mode` VARCHAR(24) NOT NULL,
  `filename` VARCHAR(255) NOT NULL,
  `owner` LONGTEXT NOT NULL,
  `expires_at` DOUBLE NOT NULL,
  PRIMARY KEY (`mode`, `filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `credential_audit_events` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `created_at` DOUBLE NOT NULL,
  `event_json` LONGTEXT NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_credential_audit_time` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `credentials` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `filename` VARCHAR(255) NOT NULL,
  `credential_data` LONGTEXT NOT NULL,
  `disabled` BIGINT DEFAULT 0,
  `error_codes` LONGTEXT DEFAULT ('[]'),
  `error_messages` LONGTEXT DEFAULT ('[]'),
  `last_success` DOUBLE,
  `user_email` LONGTEXT,
  `model_cooldowns` LONGTEXT DEFAULT ('{}'),
  `preview` BIGINT DEFAULT 1,
  `tier` LONGTEXT DEFAULT ('pro'),
  `rotation_order` BIGINT DEFAULT 0,
  `call_count` BIGINT DEFAULT 0,
  `created_at` DOUBLE DEFAULT (UNIX_TIMESTAMP()),
  `updated_at` DOUBLE DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`id`),
  KEY `idx_rotation_order` (`rotation_order`),
  KEY `idx_disabled` (`disabled`),
  UNIQUE KEY `uq_filename` (`filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_credentials` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `filename` VARCHAR(255) NOT NULL,
  `credential_data` LONGTEXT NOT NULL,
  `disabled` BIGINT DEFAULT 0,
  `error_codes` LONGTEXT DEFAULT ('[]'),
  `error_messages` LONGTEXT DEFAULT ('[]'),
  `last_success` DOUBLE,
  `user_email` LONGTEXT,
  `model_cooldowns` LONGTEXT DEFAULT ('{}'),
  `tier` LONGTEXT DEFAULT ('pro'),
  `enable_credit` BIGINT DEFAULT 0,
  `usage_limit_disabled` BIGINT NOT NULL DEFAULT 0,
  `usage_resume_at` BIGINT NOT NULL DEFAULT 0,
  `usage_limit_overrides` LONGTEXT NOT NULL DEFAULT ('{}'),
  `transient_hold_until` DOUBLE NOT NULL DEFAULT 0,
  `transient_error_count` BIGINT NOT NULL DEFAULT 0,
  `transient_error_at` DOUBLE NOT NULL DEFAULT 0,
  `proxy_mode` LONGTEXT NOT NULL DEFAULT ('inherit'),
  `proxy_url` VARCHAR(700),
  `proxy_group_id` BIGINT,
  `bound_proxy_node_id` BIGINT,
  `fixed_proxy_ip` VARCHAR(255),
  `proxy_bound_at` DOUBLE,
  `rotation_order` BIGINT DEFAULT 0,
  `call_count` BIGINT DEFAULT 0,
  `routing_window_started_at` DOUBLE,
  `routing_window_count` BIGINT DEFAULT 0,
  `routing_turn` BIGINT DEFAULT 0,
  `created_at` DOUBLE DEFAULT (UNIX_TIMESTAMP()),
  `updated_at` DOUBLE DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_ag_fixed_proxy_ip` (`fixed_proxy_ip`),
  KEY `idx_ag_bound_proxy_node` (`bound_proxy_node_id`),
  KEY `idx_ag_proxy_group` (`proxy_group_id`),
  KEY `idx_ag_routing_turn` (`routing_turn`),
  KEY `idx_ag_rotation_order` (`rotation_order`),
  KEY `idx_ag_disabled` (`disabled`),
  UNIQUE KEY `uq_filename` (`filename`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_warmup_accounts` (
  `credential_name` VARCHAR(255) NOT NULL,
  `status` VARCHAR(32) NOT NULL DEFAULT 'off',
  `level` BIGINT NOT NULL DEFAULT 0,
  `success_count` BIGINT NOT NULL DEFAULT 0,
  `window_started_at` DOUBLE,
  `next_start_at` DOUBLE NOT NULL DEFAULT 0,
  `config_json` LONGTEXT NOT NULL DEFAULT ('{}'),
  `created_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`credential_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_warmup_ip_groups` (
  `ip_key` VARCHAR(255) NOT NULL,
  `level` BIGINT NOT NULL DEFAULT 0,
  `success_count` BIGINT NOT NULL DEFAULT 0,
  `window_started_at` DOUBLE,
  `next_start_at` DOUBLE NOT NULL DEFAULT 0,
  `config_json` LONGTEXT NOT NULL DEFAULT ('{}'),
  `created_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`ip_key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_warmup_leases` (
  `lease_id` VARCHAR(255) NOT NULL,
  `credential_name` VARCHAR(255) NOT NULL,
  `ip_key` VARCHAR(255) NOT NULL,
  `created_at` DOUBLE NOT NULL,
  `expires_at` DOUBLE NOT NULL,
  PRIMARY KEY (`lease_id`),
  KEY `idx_warmup_leases_ip` (`ip_key`, `expires_at`),
  KEY `idx_warmup_leases_account` (`credential_name`, `expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `proxy_groups` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `name` VARCHAR(255) NOT NULL,
  `description` LONGTEXT NOT NULL DEFAULT (''),
  `strategy` LONGTEXT NOT NULL DEFAULT ('round_robin'),
  `enabled` BIGINT NOT NULL DEFAULT 1,
  `exclusive_assignment` BIGINT NOT NULL DEFAULT 1,
  `cursor` BIGINT NOT NULL DEFAULT 0,
  `created_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_name` (`name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `proxy_group_nodes` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `group_id` BIGINT NOT NULL,
  `name` LONGTEXT NOT NULL DEFAULT (''),
  `proxy_url` VARCHAR(700) NOT NULL,
  `enabled` BIGINT NOT NULL DEFAULT 1,
  `failure_count` BIGINT NOT NULL DEFAULT 0,
  `cooldown_until` DOUBLE,
  `last_used_at` DOUBLE,
  `expires_at` DOUBLE,
  `created_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`id`),
  KEY `idx_proxy_group_nodes_group` (`group_id`, `enabled`, `id`),
  UNIQUE KEY `uq_group_id_proxy_url` (`group_id`, `proxy_url`),
  FOREIGN KEY (`group_id`) REFERENCES `proxy_groups` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_proxy_import_leases` (
  `lease_id` VARCHAR(255) NOT NULL,
  `node_id` BIGINT NOT NULL,
  `egress_ip` VARCHAR(255) NOT NULL,
  `expires_at` DOUBLE NOT NULL,
  PRIMARY KEY (`lease_id`),
  UNIQUE KEY `uq_egress_ip` (`egress_ip`),
  UNIQUE KEY `uq_node_id` (`node_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_account_health` (
  `credential_name` VARCHAR(255) NOT NULL,
  `egress_ip` LONGTEXT,
  `egress_country` LONGTEXT,
  `binding_status` LONGTEXT NOT NULL DEFAULT ('unchecked'),
  `eligibility_status` LONGTEXT NOT NULL DEFAULT ('unchecked'),
  `eligibility_reason` LONGTEXT,
  `eligibility_checked_at` DOUBLE,
  `last_403_category` LONGTEXT,
  `last_403_reason` LONGTEXT,
  `last_403_at` DOUBLE,
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`credential_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_egress_reservations` (
  `credential_name` VARCHAR(255) NOT NULL,
  `egress_ip` VARCHAR(255) NOT NULL,
  `deleted_at` DOUBLE NOT NULL,
  `expires_at` DOUBLE NOT NULL,
  PRIMARY KEY (`credential_name`, `egress_ip`),
  KEY `idx_ag_egress_reservations_ip` (`egress_ip`, `expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `config` (
  `key` VARCHAR(255) NOT NULL,
  `value` LONGTEXT NOT NULL,
  `updated_at` DOUBLE DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `api_keys` (
  `id` VARCHAR(64) NOT NULL,
  `name` VARCHAR(255) COLLATE utf8mb4_0900_as_ci NOT NULL,
  `key_prefix` VARCHAR(255) NOT NULL,
  `secret_hash` VARCHAR(255),
  `secret_ciphertext` LONGTEXT,
  `kind` VARCHAR(32) NOT NULL,
  `status` VARCHAR(32) NOT NULL DEFAULT 'active',
  `quota_amount` LONGTEXT,
  `quota_used` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `quota_tokens_used` BIGINT NOT NULL DEFAULT 0,
  `currency` VARCHAR(16) NOT NULL DEFAULT 'CNY',
  `reset_mode` LONGTEXT NOT NULL DEFAULT ('manual'),
  `quota_period_start` LONGTEXT NOT NULL,
  `expires_at` DOUBLE,
  `last_used_at` DOUBLE,
  `last_reset_at` DOUBLE,
  `revoked_at` DOUBLE,
  `created_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  `claude_credential_name` LONGTEXT NOT NULL DEFAULT (''),
  `prompt_fusion_mode` LONGTEXT NOT NULL DEFAULT ('inherit'),
  `allowed_model` LONGTEXT NOT NULL DEFAULT (''),
  `limit_enabled` BIGINT NOT NULL DEFAULT 0,
  `day_rpm` BIGINT,
  `day_total` BIGINT,
  `night_rpm` BIGINT,
  `night_total` BIGINT,
  `limit_period_start` DOUBLE,
  `limit_period_used` BIGINT NOT NULL DEFAULT 0,
  PRIMARY KEY (`id`),
  KEY `idx_api_keys_status` (`status`, `kind`, `created_at`),
  UNIQUE KEY `uq_secret_hash` (`secret_hash`),
  UNIQUE KEY `uq_key_prefix` (`key_prefix`),
  UNIQUE KEY `uq_name` (`name`),
  CHECK (kind IN ('managed', 'environment')),
  CHECK (status IN ('active', 'disabled', 'revoked')),
  CHECK (reset_mode IN ('manual', 'monthly')),
  CHECK ((kind = 'environment' AND secret_hash IS NULL) OR (kind = 'managed' AND secret_hash IS NOT NULL))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `api_key_quota_events` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `api_key_id` VARCHAR(64) NOT NULL,
  `event_type` LONGTEXT NOT NULL,
  `amount_before` LONGTEXT NOT NULL,
  `amount_after` LONGTEXT NOT NULL,
  `created_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`id`),
  KEY `idx_api_key_quota_events_key` (`api_key_id`, `created_at`),
  FOREIGN KEY (`api_key_id`) REFERENCES `api_keys` (`id`) ON DELETE RESTRICT,
  CHECK (event_type IN ('manual_reset', 'monthly_reset'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `billing_prices` (
  `model` VARCHAR(255) NOT NULL,
  `input_price` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `output_price` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `cache_price` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `currency` VARCHAR(16) NOT NULL DEFAULT 'CNY',
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`model`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `billing_daily` (
  `billing_date` VARCHAR(10) NOT NULL,
  `api_key_id` VARCHAR(64) NOT NULL DEFAULT 'env',
  `credential_name` VARCHAR(255) NOT NULL,
  `model` VARCHAR(255) NOT NULL,
  `currency` VARCHAR(16) NOT NULL,
  `input_tokens` BIGINT NOT NULL DEFAULT 0,
  `output_tokens` BIGINT NOT NULL DEFAULT 0,
  `cache_tokens` BIGINT NOT NULL DEFAULT 0,
  `thought_tokens` BIGINT NOT NULL DEFAULT 0,
  `total_tokens` BIGINT NOT NULL DEFAULT 0,
  `input_cost` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `output_cost` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `cache_cost` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `total_cost` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `success_count` BIGINT NOT NULL DEFAULT 0,
  `failed_count` BIGINT NOT NULL DEFAULT 0,
  `unknown_usage_count` BIGINT NOT NULL DEFAULT 0,
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`billing_date`, `api_key_id`, `credential_name`, `model`, `currency`),
  KEY `idx_billing_daily_api_key` (`api_key_id`, `billing_date`),
  KEY `idx_billing_daily_model` (`model`, `billing_date`),
  KEY `idx_billing_daily_credential` (`credential_name`, `billing_date`),
  KEY `idx_billing_daily_date` (`billing_date`),
  FOREIGN KEY (`api_key_id`) REFERENCES `api_keys` (`id`) ON DELETE RESTRICT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `billing_request_dedupe` (
  `request_id` VARCHAR(255) NOT NULL,
  `created_at` DOUBLE NOT NULL,
  `expires_at` DOUBLE NOT NULL,
  PRIMARY KEY (`request_id`),
  KEY `idx_billing_dedupe_expiry` (`expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `credential_model_stats` (
  `mode` VARCHAR(24) NOT NULL,
  `credential_name` VARCHAR(255) NOT NULL,
  `model_name` VARCHAR(160) NOT NULL,
  `success_count` BIGINT NOT NULL DEFAULT 0,
  `failed_count` BIGINT NOT NULL DEFAULT 0,
  `last_status` BIGINT,
  `upstream_total_seconds` DOUBLE NOT NULL DEFAULT 0,
  `upstream_timed_count` BIGINT NOT NULL DEFAULT 0,
  `gateway_total_seconds` DOUBLE NOT NULL DEFAULT 0,
  `gateway_timed_count` BIGINT NOT NULL DEFAULT 0,
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`mode`, `credential_name`, `model_name`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `credential_model_stats_daily` (
  `mode` VARCHAR(24) NOT NULL,
  `stat_date` VARCHAR(10) NOT NULL,
  `credential_name` VARCHAR(255) NOT NULL,
  `model_name` VARCHAR(160) NOT NULL,
  `success_count` BIGINT NOT NULL DEFAULT 0,
  `failed_count` BIGINT NOT NULL DEFAULT 0,
  `last_status` BIGINT,
  `upstream_total_seconds` DOUBLE NOT NULL DEFAULT 0,
  `upstream_timed_count` BIGINT NOT NULL DEFAULT 0,
  `gateway_total_seconds` DOUBLE NOT NULL DEFAULT 0,
  `gateway_timed_count` BIGINT NOT NULL DEFAULT 0,
  `updated_at` DOUBLE NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`mode`, `stat_date`, `credential_name`, `model_name`),
  KEY `idx_model_stats_daily_date` (`mode`, `stat_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `call_records` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `request_id` LONGTEXT NOT NULL,
  `created_at` DOUBLE NOT NULL,
  `api_key_id` VARCHAR(64) NOT NULL DEFAULT 'env',
  `credential_name` VARCHAR(255) NOT NULL DEFAULT '',
  `channel` LONGTEXT NOT NULL DEFAULT ('antigravity'),
  `model_name` VARCHAR(160) NOT NULL DEFAULT '',
  `request_model` LONGTEXT NOT NULL DEFAULT (''),
  `upstream_model` VARCHAR(160) NOT NULL DEFAULT '',
  `task_type` LONGTEXT NOT NULL DEFAULT ('chat'),
  `success` BIGINT NOT NULL DEFAULT 0,
  `status_code` BIGINT,
  `gateway_seconds` DOUBLE,
  `input_tokens` BIGINT NOT NULL DEFAULT 0,
  `output_tokens` BIGINT NOT NULL DEFAULT 0,
  `cache_tokens` BIGINT NOT NULL DEFAULT 0,
  `thought_tokens` BIGINT NOT NULL DEFAULT 0,
  `total_tokens` BIGINT NOT NULL DEFAULT 0,
  `total_cost` LONGTEXT NOT NULL DEFAULT ('0.00000000'),
  `currency` VARCHAR(16) NOT NULL DEFAULT 'CNY',
  `request_features` LONGTEXT NOT NULL DEFAULT ('{}'),
  `request_details` LONGBLOB NOT NULL DEFAULT ('{}'),
  `timing_id` VARCHAR(255),
  `upstream_first_byte_seconds` DOUBLE,
  `downstream_first_byte_seconds` DOUBLE,
  `response_total_seconds` DOUBLE,
  `upstream_response_seconds` DOUBLE,
  `downstream_send_seconds` DOUBLE,
  `timing_details` LONGBLOB,
  PRIMARY KEY (`id`),
  KEY `idx_call_records_timing_id` (`timing_id`),
  KEY `idx_call_records_cred_created` (`credential_name`, `created_at`),
  KEY `idx_call_records_key_created` (`api_key_id`, `created_at`),
  KEY `idx_call_records_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_telemetry_profiles` (
  `account_id` BIGINT NOT NULL,
  `subject_ref` VARCHAR(255) NOT NULL,
  `installation_id` VARCHAR(255) NOT NULL,
  `mode` VARCHAR(24) NOT NULL DEFAULT 'off',
  `policy_revision` LONGTEXT NOT NULL,
  `environment_json` LONGTEXT NOT NULL,
  `last_outcome` LONGTEXT NOT NULL DEFAULT (''),
  `last_http_status` BIGINT,
  `last_event_at` DOUBLE,
  `updated_at` DOUBLE NOT NULL,
  PRIMARY KEY (`account_id`),
  UNIQUE KEY `uq_installation_id` (`installation_id`),
  UNIQUE KEY `uq_subject_ref` (`subject_ref`),
  CHECK (mode IN ('off','preview','send'))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `antigravity_telemetry_events` (
  `event_id` VARCHAR(255) NOT NULL,
  `timing_id` VARCHAR(255) NOT NULL,
  `account_id` BIGINT NOT NULL,
  `filename` VARCHAR(255) NOT NULL,
  `mode` VARCHAR(24) NOT NULL,
  `outcome` LONGTEXT NOT NULL,
  `http_status` BIGINT,
  `attempts` BIGINT NOT NULL,
  `event_json` LONGTEXT NOT NULL,
  `binding_json` LONGTEXT NOT NULL,
  `created_at` DOUBLE NOT NULL,
  `updated_at` DOUBLE NOT NULL,
  PRIMARY KEY (`event_id`),
  KEY `telemetry_events_timing` (`timing_id`),
  KEY `telemetry_events_created` (`created_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `refresh_import_items` (
  `id` BIGINT NOT NULL AUTO_INCREMENT,
  `batch_id` VARCHAR(255) NOT NULL,
  `position` BIGINT NOT NULL,
  `status` VARCHAR(32) NOT NULL,
  `message` LONGTEXT,
  `payload_ciphertext` LONGTEXT,
  `token_digest` VARCHAR(255),
  `proxy_group_id` BIGINT,
  `has_custom_proxy` BIGINT NOT NULL DEFAULT 0,
  `file_path` LONGTEXT,
  `email` LONGTEXT,
  `egress_ip` LONGTEXT,
  `lease_id` LONGTEXT,
  `lease_until` BIGINT,
  `retry_count` BIGINT NOT NULL DEFAULT 0,
  `retry_after` BIGINT,
  `created_at` BIGINT NOT NULL DEFAULT (UNIX_TIMESTAMP()),
  PRIMARY KEY (`id`),
  KEY `idx_refresh_import_token` (`token_digest`),
  KEY `idx_refresh_import_ready` (`status`, `id`),
  UNIQUE KEY `uq_batch_id_position` (`batch_id`, `position`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS `api_key_recent_requests` (
  `key_id` VARCHAR(64) NOT NULL,
  `admitted_at` DOUBLE NOT NULL,
  KEY `idx_key_recent_requests` (`key_id`, `admitted_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS main_write_lock (
  id INTEGER PRIMARY KEY
) ENGINE=InnoDB;

INSERT INTO main_write_lock VALUES (1) ON DUPLICATE KEY UPDATE id=1;

CREATE TABLE IF NOT EXISTS account_usage (
  account VARCHAR(255) NOT NULL,
  bucket BIGINT NOT NULL,
  requests BIGINT NOT NULL,
  generated_tokens BIGINT NOT NULL,
  PRIMARY KEY (account, bucket),
  KEY account_usage_bucket (bucket)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS fusion (
  `key` VARCHAR(255) PRIMARY KEY,
  value LONGTEXT NOT NULL,
  expires DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS leases (
  `key` VARCHAR(255) PRIMARY KEY,
  owner VARCHAR(255) NOT NULL,
  expires DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;

CREATE TABLE IF NOT EXISTS antigravity_relay_failures (
  id BIGINT AUTO_INCREMENT PRIMARY KEY,
  created_at DOUBLE NOT NULL,
  credential_name VARCHAR(255) NOT NULL,
  status_code INT NULL,
  tag VARCHAR(64) NOT NULL,
  reason TEXT NOT NULL,
  KEY idx_relay_failure_time (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_bin;
