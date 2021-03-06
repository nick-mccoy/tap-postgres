#!/usr/bin/env python3
# pylint: disable=missing-docstring,not-an-iterable,too-many-locals,too-many-arguments,invalid-name,too-many-return-statements,too-many-branches,len-as-condition

import datetime
import pdb
import json
import os
import sys
import time
import collections
import itertools
from itertools import dropwhile
import copy
import ssl
import psycopg2
import psycopg2.extras
import singer
import singer.metrics as metrics
import singer.schema
from singer import utils, metadata, get_bookmark
from singer.schema import Schema
from singer.catalog import Catalog, CatalogEntry

import tap_postgres.sync_strategies.logical_replication as logical_replication
import tap_postgres.sync_strategies.full_table as full_table
import tap_postgres.db as post_db
LOGGER = singer.get_logger()

#LogMiner do not support LONG, LONG RAW, CLOB, BLOB, NCLOB, ADT, or COLLECTION datatypes.
Column = collections.namedtuple('Column', [
    "column_name",
    "is_primary_key",
    "sql_data_type",
    "character_maximum_length",
    "numeric_precision",
    "numeric_scale"
])


REQUIRED_CONFIG_KEYS = [
    # 'database',
    'host',
    'port',
    'user',
    'password',
    'default_replication_method'
]


INTEGER_TYPES = {'integer', 'smallint', 'bigint'}
FLOAT_TYPES = {'real', 'double precision'}
JSON_TYPES = {'json', 'jsonb'}

#NB> numeric/decimal columns in postgres without a specified scale && precision
#default to 'up to 131072 digits before the decimal point; up to 16383
#digits after the decimal point'. For practical reasons, we are capping this at 74/38
#  https://www.postgresql.org/docs/10/static/datatype-numeric.html#DATATYPE-NUMERIC-TABLE
MAX_SCALE = 38
MAX_PRECISION = 100

def nullable_column(col_type, pk):
    if pk:
        return  [col_type]
    return ['null', col_type]

def schema_for_column(c):
    data_type = c.sql_data_type.lower()
    result = Schema()

    if data_type in INTEGER_TYPES:
        result.type = nullable_column('integer', c.is_primary_key)
        result.minimum = -1 * (2**(c.numeric_precision - 1))
        result.maximum = 2**(c.numeric_precision - 1) - 1
        return result

    elif data_type == 'bit' and c.character_maximum_length == 1:
        result.type = nullable_column('boolean', c.is_primary_key)
        return result

    elif data_type == 'boolean':
        result.type = nullable_column('boolean', c.is_primary_key)
        return result

    elif data_type == 'uuid':
        result.type = nullable_column('string', c.is_primary_key)
        return result

    elif data_type == 'hstore':
        result.type = nullable_column('string', c.is_primary_key)
        return result

    elif data_type in JSON_TYPES:
        result.type = nullable_column('string', c.is_primary_key)
        return result

    elif data_type == 'numeric':
        result.type = nullable_column('number', c.is_primary_key)
        if c.numeric_scale is None or c.numeric_scale > MAX_SCALE:
            LOGGER.warning('capping decimal scale to 38.  THIS MAY CAUSE TRUNCATION')
            scale = MAX_SCALE
        else:
            scale = c.numeric_scale

        if c.numeric_precision is None or c.numeric_precision > MAX_PRECISION:
            LOGGER.warning('capping decimal precision to 100.  THIS MAY CAUSE TRUNCATION')
            precision = MAX_PRECISION
        else:
            precision = c.numeric_precision

        result.exclusiveMaximum = True
        result.maximum = 10 ** (precision - scale)
        result.multipleOf = 10 ** (0 - scale)
        result.exclusiveMinimum = True
        result.minimum = -10 ** (precision - scale)
        return result

    elif data_type in {'time without time zone', 'time with time zone'}:
        #times are treated as ordinary strings as they can not possible match RFC3339
        result.type = nullable_column('string', c.is_primary_key)
        return result

    elif data_type in ('date', 'timestamp without time zone', 'timestamp with time zone'):
        result.type = nullable_column('string', c.is_primary_key)

        result.format = 'date-time'
        return result

    elif data_type in FLOAT_TYPES:
        result.type = nullable_column('number', c.is_primary_key)
        return result

    elif data_type == 'text':
        result.type = nullable_column('string', c.is_primary_key)
        return result

    elif data_type == 'character varying':
        result.type = nullable_column('string', c.is_primary_key)
        result.maxLength = c.character_maximum_length
        return result

    elif data_type == 'character':
        result.type = nullable_column('string', c.is_primary_key)
        result.maxLength = c.character_maximum_length
        return result

    return Schema(None)

def produce_table_info(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        table_info = {}
        cur.execute("""
SELECT
  pg_class.reltuples::BIGINT             AS approximate_row_count,
  pg_class.relkind = 'v'                 AS is_view,
  n.nspname                              AS schema_name,
  pg_class.relname                       AS table_name,
  attname                                AS column_name,
  i.indisprimary                         AS primary_key,
  format_type(a.atttypid, NULL::integer) AS data_type,
  information_schema._pg_char_max_length(information_schema._pg_truetypid(a.*, t.*),
                                         information_schema._pg_truetypmod(a.*, t.*))::information_schema.cardinal_number AS character_maximum_length,
  information_schema._pg_numeric_precision(information_schema._pg_truetypid(a.*, t.*),
                                           information_schema._pg_truetypmod(a.*, t.*))::information_schema.cardinal_number AS numeric_precision,
 information_schema._pg_numeric_scale(information_schema._pg_truetypid(a.*, t.*),
                                      information_schema._pg_truetypmod(a.*, t.*))::information_schema.cardinal_number AS numeric_scale
FROM   pg_attribute a
LEFT JOIN pg_type t on a.atttypid = t.oid
JOIN pg_class
  ON pg_class.oid = a.attrelid
JOIN pg_catalog.pg_namespace n
  ON n.oid = pg_class.relnamespace
left outer join  pg_index as i
  on a.attrelid = i.indrelid
 and a.attnum = ANY(i.indkey)
WHERE attnum > 0
AND NOT a.attisdropped
AND pg_class.relkind IN ('r', 'v')
AND n.nspname NOT in ('pg_toast', 'pg_catalog', 'information_schema')
AND has_table_privilege(pg_class.oid, 'SELECT') = true """)
        for row in cur.fetchall():
            row_count, is_view, schema_name, table_name, *col_info = row

            if table_info.get(schema_name) is None:
                table_info[schema_name] = {}

            if table_info[schema_name].get(table_name) is None:
                table_info[schema_name][table_name] = {'is_view': is_view, 'row_count' : row_count, 'columns' : {}}

            col_name = col_info[0]
            table_info[schema_name][table_name]['columns'][col_name] = Column(*col_info)

        return table_info

def get_database_name(connection):
    cur = connection.cursor()

    rows = cur.execute("SELECT name FROM v$database").fetchall()
    return rows[0][0]

def write_sql_data_type_md(mdata, col_info):
    c_name = col_info.column_name
    if col_info.sql_data_type == 'bit' and col_info.character_maximum_length > 1:
        mdata = metadata.write(mdata, ('properties', c_name), 'sql-datatype', "bit({})".format(col_info.character_maximum_length))
    else:
        mdata = metadata.write(mdata, ('properties', c_name), 'sql-datatype', col_info.sql_data_type)

    return mdata

def discover_columns(connection, table_info):
    entries = []
    for schema_name in table_info.keys():
        for table_name in table_info[schema_name].keys():
            mdata = {}
            columns = table_info[schema_name][table_name]['columns']
            table_pks = [col_name for col_name, col_info in columns.items() if col_info.is_primary_key]
            with connection.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(" SELECT current_database()")
                database_name = cur.fetchone()[0]

            metadata.write(mdata, (), 'table-key-properties', table_pks)
            metadata.write(mdata, (), 'schema-name', schema_name)
            metadata.write(mdata, (), 'database-name', database_name)
            metadata.write(mdata, (), 'row-count', table_info[schema_name][table_name]['row_count'])
            metadata.write(mdata, (), 'is-view', table_info[schema_name][table_name].get('is_view'))

            column_schemas = {col_name : schema_for_column(col_info) for col_name, col_info in columns.items()}
            schema = Schema(type='object', properties=column_schemas)
            for c_name in column_schemas.keys():
                mdata = write_sql_data_type_md(mdata, columns[c_name])
                if column_schemas[c_name].type is None:
                    mdata = metadata.write(mdata, ('properties', c_name), 'inclusion', 'unsupported')
                    mdata = metadata.write(mdata, ('properties', c_name), 'selected-by-default', False)
                elif table_info[schema_name][table_name]['columns'][c_name].is_primary_key:
                    mdata = metadata.write(mdata, ('properties', c_name), 'inclusion', 'automatic')
                    mdata = metadata.write(mdata, ('properties', c_name), 'selected-by-default', True)
                else:
                    mdata = metadata.write(mdata, ('properties', c_name), 'inclusion', 'available')
                    mdata = metadata.write(mdata, ('properties', c_name), 'selected-by-default', True)

            entry = CatalogEntry(
                table=table_name,
                stream=table_name,
                metadata=metadata.to_list(mdata),
                tap_stream_id=database_name + '-' + schema_name + '-' + table_name,
                schema=schema)

            entries.append(entry)

    return entries

def dump_catalog(catalog):
    catalog.dump()

def discover_db(connection):
    table_info = produce_table_info(connection)
    db_streams = discover_columns(connection, table_info)
    return db_streams

def do_discovery(conn_config):
    all_streams = []
    all_dbs = []
    with post_db.open_connection(conn_config) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            LOGGER.info("Fetching db's from cluster")
            cur.execute("""
            SELECT datname
              FROM pg_database
              WHERE datistemplate = false
                AND CASE WHEN version() LIKE '%Redshift%' THEN true
                         ELSE has_database_privilege(datname,'CONNECT')
                    END = true """)
            all_dbs = cur.fetchall()

    for db_row in all_dbs:
        dbname = db_row[0]
        LOGGER.info("Discovering db %s", dbname)
        conn_config['dbname'] = dbname
        with post_db.open_connection(conn_config) as conn:
            db_streams = discover_db(conn)
            all_streams = all_streams + db_streams

    cluster_catalog = Catalog(all_streams)
    dump_catalog(cluster_catalog)
    return cluster_catalog

def should_sync_column(md_map, field_name):
    if md_map.get(('properties', field_name), {}).get('inclusion') == 'unsupported':
        return False

    if md_map.get(('properties', field_name), {}).get('selected'):
        return True

    if md_map.get(('properties', field_name), {}).get('inclusion') == 'automatic':
        return True

    return False

def send_schema_message(stream, bookmark_properties):
    s_md = metadata.to_map(stream.metadata)
    if s_md.get((), {}).get('is-view'):
        key_properties = s_md.get((), {}).get('view-key-properties')
    else:
        key_properties = s_md.get((), {}).get('table-key-properties')


    schema_message = singer.SchemaMessage(stream=stream.stream,
                                          schema=stream.schema.to_dict(),
                                          key_properties=key_properties,
                                          bookmark_properties=bookmark_properties)
    singer.write_message(schema_message)

def is_selected_via_metadata(stream):
    table_md = metadata.to_map(stream.metadata).get((), {})
    return table_md.get('selected')

def do_sync_full_table(conn_config, stream, state, desired_columns, md_map):
    LOGGER.info("Stream %s is using full_table", stream.tap_stream_id)
    send_schema_message(stream, [])
    if md_map.get((), {}).get('is-view'):
        state = full_table.sync_view(conn_config, stream, state, desired_columns, md_map)
    else:
        state = full_table.sync_table(conn_config, stream, state, desired_columns, md_map)
    return state

def do_sync_logical_replication(conn_config, stream, state, desired_columns, md_map):
    if get_bookmark(state, stream.tap_stream_id, 'lsn'):
        LOGGER.info("Stream %s is using logical replication. end lsn %s", stream.tap_stream_id, logical_replication.fetch_current_lsn(conn_config))
        logical_replication.add_automatic_properties(stream)
        send_schema_message(stream, ['lsn'])
        state = logical_replication.sync_table(conn_config, stream, state, desired_columns, md_map)
    else:
        #start off with full-table replication
        end_lsn = logical_replication.fetch_current_lsn(conn_config)
        LOGGER.info("Stream %s is using logical replication. performing initial full table sync", stream.tap_stream_id)
        send_schema_message(stream, [])
        state = full_table.sync_table(conn_config, stream, state, desired_columns, md_map)
        state = singer.write_bookmark(state,
                                      stream.tap_stream_id,
                                      'xmin',
                                      None)
        #once we are done with full table, write the lsn to the state
        state = singer.write_bookmark(state, stream.tap_stream_id, 'lsn', end_lsn)

    return state

def do_sync(conn_config, catalog, default_replication_method, state):
    streams = list(filter(is_selected_via_metadata, catalog.streams))
    streams.sort(key=lambda s: s.tap_stream_id)

    currently_syncing = singer.get_currently_syncing(state)

    if currently_syncing:
        streams = dropwhile(lambda s: s.tap_stream_id != currently_syncing, streams)

    for stream in streams:
        md_map = metadata.to_map(stream.metadata)
        conn_config['dbname'] = md_map.get(()).get('database-name')
        state = singer.set_currently_syncing(state, stream.tap_stream_id)


        desired_columns = [c for c in stream.schema.properties.keys() if should_sync_column(md_map, c)]
        desired_columns.sort()

        if len(desired_columns) == 0:
            LOGGER.warning('There are no columns selected for stream %s, skipping it', stream.tap_stream_id)
            continue

        replication_method = md_map.get((), {}).get('replication-method', default_replication_method)
        if replication_method == 'LOG_BASED' and md_map.get((), {}).get('is-view'):
            LOGGER.warning('Logical Replication is NOT supported for views. skipping stream %s', stream.tap_stream_id)
            continue


        if replication_method == 'LOG_BASED':
            state = do_sync_logical_replication(conn_config, stream, state, desired_columns, md_map)
        elif replication_method == 'FULL_TABLE':
            state = do_sync_full_table(conn_config, stream, state, desired_columns, md_map)
        else:
            raise Exception("only LOG_BASED and FULL_TABLE are supported right now :)")

        state = singer.set_currently_syncing(state, None)
        singer.write_message(singer.StateMessage(value=copy.deepcopy(state)))


def main_impl():
    args = utils.parse_args(REQUIRED_CONFIG_KEYS)
    conn_config = {'host'     : args.config['host'],
                   'user'     : args.config['user'],
                   'password' : args.config['password'],
                   'port'     : args.config['port'],
                   'dbname'   : args.config['dbname']}

    if args.discover:
        do_discovery(conn_config)
    elif args.catalog:
        state = args.state
        do_sync(conn_config, args.catalog, args.config.get('default_replication_method'), state)
    else:
        LOGGER.info("No properties were selected")

def main():
    try:
        main_impl()
    except Exception as exc:
        LOGGER.critical(exc)
        raise exc
