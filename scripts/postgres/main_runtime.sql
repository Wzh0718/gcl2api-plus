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
