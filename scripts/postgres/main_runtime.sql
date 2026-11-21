ALTER TABLE antigravity_credentials ADD COLUMN IF NOT EXISTS routing_bonus_turn BIGINT;
CREATE INDEX IF NOT EXISTS idx_ag_bonus_turn ON antigravity_credentials(routing_bonus_turn);
CREATE TABLE IF NOT EXISTS routing_pending (id TEXT PRIMARY KEY, account TEXT NOT NULL, started BIGINT NOT NULL);
CREATE INDEX IF NOT EXISTS routing_pending_account ON routing_pending(account, started);
CREATE INDEX IF NOT EXISTS routing_pending_started ON routing_pending(started);

-- Keep full minutes cheap to rank. The partial minute at the left edge of
-- the rolling window is still read from account_usage at second precision.
CREATE TABLE IF NOT EXISTS routing_minutes (
    account text NOT NULL, minute bigint NOT NULL, tokens bigint NOT NULL,
    PRIMARY KEY(account, minute)
) WITH (fillfactor=80);
CREATE INDEX IF NOT EXISTS routing_minutes_expiry ON routing_minutes(minute);

CREATE OR REPLACE FUNCTION gcli_update_routing_minutes() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    delta bigint;
    bucket_start bigint;
    name text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        name := OLD.account;
        bucket_start := OLD.bucket / 60 * 60;
        delta := -OLD.generated_tokens;
    ELSE
        name := NEW.account;
        bucket_start := NEW.bucket / 60 * 60;
        delta := NEW.generated_tokens;
        IF TG_OP = 'UPDATE' THEN
            delta := delta - OLD.generated_tokens;
        END IF;
    END IF;
    IF delta = 0 OR bucket_start + 60 <= floor(extract(epoch from statement_timestamp()))::bigint - 3600 THEN
        RETURN NULL;
    END IF;
    UPDATE routing_minutes SET tokens=tokens+delta WHERE account=name AND minute=bucket_start;
    IF NOT FOUND THEN
        INSERT INTO routing_minutes(account, minute, tokens) VALUES(name, bucket_start, delta)
        ON CONFLICT(account, minute) DO UPDATE SET tokens=routing_minutes.tokens + EXCLUDED.tokens;
        DELETE FROM routing_minutes WHERE (account, minute) IN (
            SELECT account, minute FROM routing_minutes
            WHERE minute <= floor(extract(epoch from statement_timestamp()))::bigint - 3660
            ORDER BY minute, account LIMIT 128 FOR UPDATE SKIP LOCKED
        );
    END IF;
    RETURN NULL;
END $$;

-- Install the trigger and seed existing usage under the same write lock, once.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid='account_usage'::regclass
                   AND tgname='gcli_routing_minutes') THEN
        LOCK TABLE account_usage IN SHARE ROW EXCLUSIVE MODE;
        INSERT INTO routing_minutes(account, minute, tokens)
        SELECT account, bucket / 60 * 60, sum(generated_tokens) FROM account_usage
        WHERE bucket > floor(extract(epoch from statement_timestamp()))::bigint - 3600
        GROUP BY account, bucket / 60 * 60;
        CREATE TRIGGER gcli_routing_minutes AFTER INSERT OR UPDATE OF generated_tokens OR DELETE
        ON account_usage FOR EACH ROW EXECUTE FUNCTION gcli_update_routing_minutes();
    END IF;
END $$;

CREATE OR REPLACE FUNCTION gcli_cooldown(document text, path text)
RETURNS double precision LANGUAGE sql IMMUTABLE STRICT AS $$
    SELECT CASE jsonb_typeof(value)
        WHEN 'number' THEN (value::text)::double precision
        WHEN 'boolean' THEN CASE WHEN value = 'true'::jsonb THEN 1 ELSE 0 END
        WHEN 'null' THEN NULL
        ELSE 'Infinity'::double precision
    END
    FROM (SELECT document::jsonb -> (substring(path from 3)::jsonb #>> '{}') AS value) AS field WHERE value IS NOT NULL
$$;

CREATE TABLE IF NOT EXISTS fusion (key text PRIMARY KEY, value text NOT NULL, expires double precision NOT NULL);
CREATE TABLE IF NOT EXISTS leases (key text PRIMARY KEY, owner text NOT NULL, expires double precision NOT NULL);

-- Serialise a key's rolling and period limits with its billing row. Keep the
-- lock inside one server call, including on a rejected admission.
CREATE OR REPLACE FUNCTION gcli_admit_key(
    requested_key text, request_time double precision, daytime boolean, period_start double precision
) RETURNS text LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    settings record;
    used bigint;
BEGIN
    SELECT limit_enabled, CASE WHEN daytime THEN day_rpm ELSE night_rpm END AS rpm,
           CASE WHEN daytime THEN day_total ELSE night_total END AS period_limit,
           limit_period_start, limit_period_used
      INTO STRICT settings FROM api_keys WHERE id = requested_key FOR UPDATE;
    IF settings.limit_enabled = 0 THEN RETURN NULL; END IF;
    used := CASE WHEN settings.limit_period_start = period_start THEN settings.limit_period_used ELSE 0 END;
    IF used >= settings.period_limit THEN RETURN '时段总额度已耗尽'; END IF;
    IF settings.rpm IS NOT NULL AND
       (SELECT count(*) FROM api_key_recent_requests
         WHERE key_id = requested_key AND admitted_at > request_time - 60) >= settings.rpm THEN
        RETURN '已达每分钟请求上限';
    END IF;
    DELETE FROM api_key_recent_requests WHERE key_id = requested_key AND admitted_at <= request_time - 60;
    INSERT INTO api_key_recent_requests (key_id, admitted_at) VALUES (requested_key, request_time);
    UPDATE api_keys SET limit_period_start = period_start, limit_period_used = used + 1 WHERE id = requested_key;
    RETURN NULL;
END;
$$;

-- Keep the ledger, idempotency claim and key quota in the caller's single transaction.
CREATE OR REPLACE FUNCTION gcli_settle_usage(
    req text, at_time double precision, expiry double precision, bill_date text,
    key_id text, account_name text, model_name text, unit text,
    tokens_in bigint, tokens_out bigint, tokens_cache bigint, tokens_thought bigint, tokens_total bigint,
    cost_in numeric, cost_out numeric, cost_cache numeric, cost_total numeric,
    succeeded bigint, failed bigint, unknown_count bigint
) RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    key_state record;
    new_month text := left(bill_date, 7) || '-01';
    reset_due boolean;
    first_entry boolean;
BEGIN
    PERFORM pg_advisory_xact_lock_shared(72210439);
    SELECT kind, reset_mode, quota_period_start, quota_used, quota_tokens_used INTO key_state
      FROM api_keys WHERE id = key_id FOR UPDATE;
    INSERT INTO billing_request_dedupe(request_id, created_at, expires_at) VALUES(req, at_time, expiry)
        ON CONFLICT(request_id) DO UPDATE SET created_at=excluded.created_at, expires_at=excluded.expires_at
        WHERE billing_request_dedupe.expires_at < excluded.created_at;
    first_entry := FOUND;
    DELETE FROM billing_request_dedupe WHERE request_id IN (
        SELECT request_id FROM billing_request_dedupe WHERE expires_at < at_time
        ORDER BY expires_at, request_id LIMIT 500 FOR UPDATE SKIP LOCKED
    );
    IF NOT first_entry THEN RETURN false; END IF;

    INSERT INTO billing_daily AS d(billing_date,api_key_id,credential_name,model,currency,
        input_tokens,output_tokens,cache_tokens,thought_tokens,total_tokens,input_cost,output_cost,
        cache_cost,total_cost,success_count,failed_count,unknown_usage_count,updated_at)
    VALUES(bill_date,key_id,account_name,model_name,unit,tokens_in,tokens_out,tokens_cache,tokens_thought,
        tokens_total,cost_in::text,cost_out::text,cost_cache::text,cost_total::text,succeeded,failed,
        unknown_count,floor(extract(epoch FROM statement_timestamp())))
    ON CONFLICT(billing_date,api_key_id,credential_name,model,currency) DO UPDATE SET
        input_tokens=d.input_tokens+tokens_in, output_tokens=d.output_tokens+tokens_out,
        cache_tokens=d.cache_tokens+tokens_cache, thought_tokens=d.thought_tokens+tokens_thought,
        total_tokens=d.total_tokens+tokens_total, input_cost=round(d.input_cost::numeric+cost_in,8)::text,
        output_cost=round(d.output_cost::numeric+cost_out,8)::text, cache_cost=round(d.cache_cost::numeric+cost_cache,8)::text,
        total_cost=round(d.total_cost::numeric+cost_total,8)::text, success_count=d.success_count+succeeded,
        failed_count=d.failed_count+failed, unknown_usage_count=d.unknown_usage_count+unknown_count,
        updated_at=excluded.updated_at;

    reset_due := key_state.kind='managed' AND key_state.reset_mode='monthly' AND key_state.quota_period_start<>new_month;
    IF reset_due THEN
        INSERT INTO api_key_quota_events(api_key_id,event_type,amount_before,amount_after,created_at)
        VALUES(key_id,'monthly_reset',round(key_state.quota_used::numeric,8)::text,'0.00000000',at_time);
    END IF;
    UPDATE api_keys SET quota_used=round((CASE WHEN reset_due THEN 0 ELSE quota_used::numeric END)+cost_total,8)::text,
        quota_tokens_used=(CASE WHEN reset_due THEN 0 ELSE greatest(quota_tokens_used,0) END)+tokens_total,
        quota_period_start=CASE WHEN reset_due THEN new_month ELSE quota_period_start END,
        last_reset_at=CASE WHEN reset_due THEN at_time ELSE last_reset_at END,last_used_at=at_time,updated_at=at_time
    WHERE id=key_id;
    RETURN true;
END;
$$;

-- 上游 5xx 的就地冷却：账号级 10 分钟窗口 + 累计降权计数。
-- 5xx 是服务端瞬时错误，不改变 disabled；窗口到期由选择器自动放回。
ALTER TABLE antigravity_credentials
    ADD COLUMN IF NOT EXISTS transient_hold_until double precision NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS transient_error_count bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS transient_error_at double precision NOT NULL DEFAULT 0;

-- Only lock rows requiring repair. An unrelated account can complete on a
-- different connection, and healthy calls do not write or take a row lock.
CREATE OR REPLACE FUNCTION gcli_record_antigravity_success(account_name text, model text)
RETURNS double precision LANGUAGE plpgsql VOLATILE AS $$
DECLARE
    started timestamptz;
    lock_ms double precision := 0;
    cred record;
    ts double precision := extract(epoch FROM statement_timestamp());
    has_errors boolean;
    expired boolean;
BEGIN
    started := clock_timestamp();
    SELECT c.error_codes, c.model_cooldowns::jsonb AS cooldowns, c.transient_error_count
      INTO cred FROM antigravity_credentials c
     WHERE c.filename = account_name AND (
         coalesce(c.error_codes, '') NOT IN ('', '[]') OR c.transient_error_count > 0
         OR (c.model_cooldowns::jsonb ? model AND CASE
             WHEN jsonb_typeof(c.model_cooldowns::jsonb -> model) = 'number'
             THEN (c.model_cooldowns::jsonb ->> model)::numeric <= ts ELSE true END)
     ) FOR NO KEY UPDATE;
    lock_ms := extract(epoch FROM clock_timestamp()-started)*1000;
    IF FOUND THEN
        has_errors := coalesce(cred.error_codes, '') NOT IN ('', '[]');
        expired := cred.cooldowns ? model AND CASE
            WHEN jsonb_typeof(cred.cooldowns -> model) = 'number'
            THEN (cred.cooldowns ->> model)::numeric <= ts ELSE true END;
        UPDATE antigravity_credentials SET
            last_success = CASE WHEN has_errors THEN floor(ts) ELSE last_success END,
            error_codes = CASE WHEN has_errors THEN '[]' ELSE error_codes END,
            error_messages = CASE WHEN has_errors THEN '{}' ELSE error_messages END,
            model_cooldowns = CASE WHEN expired THEN (cred.cooldowns - model)::text ELSE model_cooldowns END,
            transient_error_at = CASE WHEN transient_error_count > 0 THEN 0 ELSE transient_error_at END,
            transient_error_count = 0, updated_at = floor(ts)
        WHERE filename = account_name;
    END IF;

    started := clock_timestamp();
    PERFORM 1 FROM antigravity_account_health WHERE credential_name = account_name AND (
        last_403_category IS NOT NULL OR binding_status IN ('proxy_drift','proxy_failed') OR
        eligibility_status IN ('geo_blocked','account_blocked','validation_required','error')
    ) FOR NO KEY UPDATE;
    lock_ms := lock_ms + extract(epoch FROM clock_timestamp()-started)*1000;
    IF FOUND THEN
        UPDATE antigravity_account_health SET
            binding_status = CASE WHEN binding_status IN ('proxy_drift','proxy_failed')
                                  THEN 'healthy' ELSE binding_status END,
            eligibility_status = 'eligible', eligibility_reason = NULL,
            eligibility_checked_at = floor(ts), last_403_category = NULL, last_403_reason = NULL,
            last_403_at = NULL, updated_at = floor(ts)
        WHERE credential_name = account_name;
    END IF;
    RETURN lock_ms;
END;
$$;
