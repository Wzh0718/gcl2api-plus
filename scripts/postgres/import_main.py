"""Copy a consistent SQLite snapshot into an empty PostgreSQL schema.

Set MAIN_POSTGRES_DSN and provision the destination schema for its login.
Rehearsals exercise real source values and roll back the entire destination.
For a cutover, stop and drain all SQLite writers before invoking --import-all.
"""
import argparse
import asyncio
from contextlib import closing
import os
from pathlib import Path
import sqlite3

import asyncpg


SQL_DIR = Path(__file__).parent
PARENT_TABLES = {"proxy_groups", "api_keys", "antigravity_credentials", "credentials"}


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


async def transfer(source, conn, schema, full):
    await conn.execute('SET LOCAL search_path TO ' + quoted(schema))
    if await conn.fetchval("SELECT current_schema()") != schema:
        raise ValueError("Destination schema must already exist and be accessible")
    if await conn.fetchval("SELECT count(*) FROM pg_class WHERE relnamespace = current_schema()::regnamespace"):
        raise ValueError("Destination schema must be empty")
    await conn.execute((SQL_DIR / 'main_tables.sql').read_text())
    pg_columns = {}
    for row in await conn.fetch("""
        SELECT table_name, column_name, data_type, is_identity FROM information_schema.columns
        WHERE table_schema=$1 ORDER BY table_name, ordinal_position
    """, schema):
        pg_columns.setdefault(row['table_name'], {})[row['column_name']] = row
    tables = [row[0] for row in source.execute("""
        SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'
        ORDER BY rowid
    """)]
    if set(tables) != set(pg_columns):
        raise ValueError("Source and destination table sets differ")
    # Referenced tables must be inserted first even if a prior migration rebuilt them.
    tables.sort(key=lambda name: name not in PARENT_TABLES)
    for table in tables:
        cols = [r[1] for r in source.execute('PRAGMA table_info(' + quoted(table) + ')')]
        if set(cols) != set(pg_columns[table]):
            raise ValueError(f"Column mismatch for {table}")
        binary = [i for i, name in enumerate(cols) if pg_columns[table][name]['data_type'] == 'bytea']
        query = 'SELECT ' + ','.join(map(quoted, cols)) + ' FROM ' + quoted(table)
        if not full and table not in PARENT_TABLES:
            query += ' LIMIT 32'
        cursor = source.execute(query)
        count = 0
        while batch := cursor.fetchmany(128):
            for i in binary:
                for j, row in enumerate(batch):
                    if isinstance(row[i], str):
                        batch[j] = (*row[:i], row[i].encode(), *row[i + 1:])
            await conn.copy_records_to_table(table, schema_name=schema, columns=cols, records=batch)
            count += len(batch)
        if await conn.fetchval('SELECT count(*) FROM ' + quoted(table)) != count:
            raise ValueError(f"Row count mismatch for {table}")
        for name, info in pg_columns[table].items():
            if info['is_identity'] != 'YES':
                continue
            maximum = await conn.fetchval('SELECT max(' + quoted(name) + ') FROM ' + quoted(table))
            old_seq = source.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
            last = max(maximum or 0, old_seq[0] if old_seq else 0)
            seq = await conn.fetchval("SELECT pg_get_serial_sequence($1,$2)", quoted(table), name)
            await conn.execute("SELECT setval($1::regclass,$2,$3)", seq, max(last, 1), last > 0)
        print(f"{table}: {count}", flush=True)
    await conn.execute((SQL_DIR / 'main_indexes.sql').read_text())


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('schema')
    parser.add_argument('--import-all', action='store_true')
    args = parser.parse_args()
    dest = await asyncpg.connect(os.environ['MAIN_POSTGRES_DSN'])
    try:
        with closing(sqlite3.connect(args.source.resolve().as_uri() + '?mode=ro', uri=True)) as source:
            source.execute('BEGIN')
            tx = dest.transaction()
            await tx.start()
            try:
                await transfer(source, dest, args.schema, args.import_all)
            except BaseException:
                await tx.rollback()
                raise
            if args.import_all:
                await tx.commit()
            else:
                await tx.rollback()
    finally:
        await dest.close()


if __name__ == '__main__':
    asyncio.run(main())
