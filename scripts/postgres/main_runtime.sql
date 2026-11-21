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
