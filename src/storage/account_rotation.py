"""Bounded token-weighted rotation. Each eligible account owns one or two tickets."""

TOKEN_GAP = 8192
PENDING_TOKENS = 2048

SQLITE_USAGE_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS account_usage (
        account TEXT NOT NULL, bucket INTEGER NOT NULL, requests INTEGER NOT NULL,
        generated_tokens INTEGER NOT NULL, PRIMARY KEY(account,bucket)) WITHOUT ROWID""",
    "CREATE INDEX IF NOT EXISTS account_usage_bucket ON account_usage(bucket)",
    """CREATE TABLE IF NOT EXISTS routing_pending (
        id TEXT PRIMARY KEY, account TEXT NOT NULL, started INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS routing_pending_account ON routing_pending(account,started)",
    "CREATE INDEX IF NOT EXISTS routing_pending_started ON routing_pending(started)",
)


# Confirmed output is recorded at settlement. Only live reservations in this
# same window contribute an estimate, including work started by another worker.
def effective_usage_sql(account, *, postgres=False):
    cutoff = "(unixepoch() - 3600)"
    boundary = f"(({cutoff} / 60) * 60)"
    seconds_filter = f"AND bucket < {boundary} + 60" if postgres else ""
    tokens = f"""COALESCE((SELECT SUM(generated_tokens) FROM account_usage
                  WHERE account = {account} AND bucket > {cutoff} {seconds_filter}), 0)"""
    if postgres:
        tokens += f""" + COALESCE((SELECT SUM(tokens) FROM routing_minutes
                        WHERE account = {account} AND minute > {boundary}), 0)"""
    return f"""{tokens} + {PENDING_TOKENS} * (SELECT COUNT(*) FROM routing_pending
                    WHERE account = {account} AND started > {cutoff})"""


# Keep the original FIFO as the regular queue. The bonus ticket has its own
# last-served turn, so an idle account cannot accumulate unbounded credit.
def tickets_sql(where_sql, *, postgres=False, pinned=False):
    joins = ""
    usage = "0"
    if not pinned:
        usage = effective_usage_sql("antigravity_credentials.filename")
    if postgres and not pinned:
        # Group once across the pool. A correlated range scan can visit every
        # account's boundary buckets once for EACH eligible account.
        cutoff = "(unixepoch() - 3600)"
        boundary = f"(({cutoff} / 60) * 60)"
        joins = f"""
            LEFT JOIN (SELECT account, SUM(tokens) AS minutes FROM routing_minutes
                       WHERE minute > {boundary} GROUP BY account) m ON m.account=filename
            LEFT JOIN (SELECT account, SUM(generated_tokens) AS seconds FROM account_usage
                       WHERE bucket > {cutoff} AND bucket < {boundary} + 60
                       GROUP BY account) s ON s.account=filename
            LEFT JOIN (SELECT account, COUNT(*) AS pending FROM routing_pending
                       WHERE started > {cutoff} GROUP BY account) p ON p.account=filename"""
        usage = f"COALESCE(minutes,0) + COALESCE(seconds,0) + {PENDING_TOKENS} * COALESCE(pending,0)"
    return f"""
        eligible AS (
            SELECT filename, COALESCE(rotation_order,0) AS rotation_order,
                   COALESCE(routing_turn,0) AS regular_turn, routing_bonus_turn,
                   {usage} AS usage
            FROM antigravity_credentials {joins} WHERE {where_sql} AND usage_resume_at <= ?
        ), tickets AS (
            SELECT filename, rotation_order, regular_turn AS ticket_turn, 0 AS bonus FROM eligible
            UNION ALL
            SELECT filename, rotation_order, COALESCE(routing_bonus_turn,regular_turn), 1
            FROM eligible WHERE usage + {TOKEN_GAP} <= (SELECT MAX(usage) FROM eligible)
        )"""


TICKET_ORDER = "ticket_turn, bonus DESC, rotation_order, filename"
LAST_TURN_SQL = """(SELECT COALESCE(MAX(turn),0) FROM (
    SELECT MAX(routing_turn) AS turn FROM antigravity_credentials UNION ALL
    SELECT MAX(routing_bonus_turn) FROM antigravity_credentials
) AS last_turns)"""
