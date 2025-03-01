#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2016-present MagicStack Inc. and the EdgeDB authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#


"""Database structure and objects supporting EdgeDB metadata."""

from __future__ import annotations
from typing import *

import re

import edb._edgeql_parser as ql_parser

from edb.common import context as parser_context
from edb.common import debug
from edb.common import exceptions
from edb.common import uuidgen
from edb.common import xdedent
from edb.common.typeutils import not_none

from edb.edgeql import ast as qlast
from edb.edgeql import qltypes
from edb.edgeql import quote as qlquote
from edb.edgeql import compiler as qlcompiler

from edb.ir import statypes

from edb.schema import constraints as s_constr
from edb.schema import links as s_links
from edb.schema import name as s_name
from edb.schema import objects as s_obj
from edb.schema import objtypes as s_objtypes
from edb.schema import pointers as s_pointers
from edb.schema import properties as s_props
from edb.schema import schema as s_schema
from edb.schema import sources as s_sources
from edb.schema import types as s_types
from edb.schema import utils as s_utils

from edb.server import defines
from edb.server import compiler as edbcompiler
from edb.server import config as edbconfig
from edb.server import pgcon  # HM.

from .resolver import sql_introspection

from . import common
from . import compiler
from . import dbops
from . import types
from . import params
from . import codegen


q = common.qname
qi = common.quote_ident
ql = common.quote_literal
qt = common.quote_type


DATABASE_ID_NAMESPACE = uuidgen.UUID('0e6fed66-204b-11e9-8666-cffd58a5240b')
CONFIG_ID_NAMESPACE = uuidgen.UUID('a48b38fa-349b-11e9-a6be-4f337f82f5ad')
CONFIG_ID = {
    None: uuidgen.UUID('172097a4-39f4-11e9-b189-9321eb2f4b97'),
    qltypes.ConfigScope.INSTANCE: uuidgen.UUID(
        '172097a4-39f4-11e9-b189-9321eb2f4b98'),
    qltypes.ConfigScope.DATABASE: uuidgen.UUID(
        '172097a4-39f4-11e9-b189-9321eb2f4b99'),
}


class PGConnection(Protocol):

    async def sql_execute(
        self,
        sql: bytes | tuple[bytes, ...],
    ) -> None:
        ...

    async def sql_fetch(
        self,
        sql: bytes | tuple[bytes, ...],
        *,
        args: tuple[bytes, ...] | list[bytes] = (),
    ) -> list[tuple[bytes, ...]]:
        ...

    async def sql_fetch_val(
        self,
        sql: bytes,
        *,
        args: tuple[bytes, ...] | list[bytes] = (),
    ) -> bytes:
        ...

    async def sql_fetch_col(
        self,
        sql: bytes,
        *,
        args: tuple[bytes, ...] | list[bytes] = (),
    ) -> list[bytes]:
        ...


class DBConfigTable(dbops.Table):
    def __init__(self) -> None:
        super().__init__(name=('edgedb', '_db_config'))

        self.add_columns([
            dbops.Column(name='name', type='text'),
            dbops.Column(name='value', type='jsonb'),
        ])

        self.add_constraint(
            dbops.UniqueConstraint(
                table_name=('edgedb', '_db_config'),
                columns=['name'],
            ),
        )


class DMLDummyTable(dbops.Table):
    """A empty dummy table used when we need to emit no-op DML.

    This is used by scan_check_ctes in the pgsql compiler to
    force the evaluation of error checking.
    """
    def __init__(self) -> None:
        super().__init__(name=('edgedb', '_dml_dummy'))

        self.add_columns([
            dbops.Column(name='id', type='int8'),
            dbops.Column(name='flag', type='bool'),
        ])

        self.add_constraint(
            dbops.UniqueConstraint(
                table_name=('edgedb', '_dml_dummy'),
                columns=['id'],
            ),
        )

    SETUP_QUERY = '''
        INSERT INTO edgedb._dml_dummy VALUES (0, false)
    '''


class BigintDomain(dbops.Domain):
    """Bigint: a variant of numeric that enforces zero digits after the dot.

    We're using an explicit scale check as opposed to simply specifying
    the numeric bounds, because using bounds severly restricts the range
    of the numeric type (1000 vs 131072 digits).
    """
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'bigint_t'),
            base='numeric',
            constraints=(
                dbops.DomainCheckConstraint(
                    domain_name=('edgedb', 'bigint_t'),
                    expr=("scale(VALUE) = 0 AND VALUE != 'NaN'"),
                ),
            ),
        )


class ConfigMemoryDomain(dbops.Domain):
    """Represents the cfg::memory type. Stores number of bytes.

    Defined just as edgedb.bigint_t:

    * numeric is used to ensure we can comfortably represent huge amounts
      of data beyond petabytes;
    * enforces zero digits after the dot.
    """
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'memory_t'),
            base='int8',
            constraints=(
                dbops.DomainCheckConstraint(
                    domain_name=('edgedb', 'memory_t'),
                    expr=("VALUE >= 0"),
                ),
            ),
        )


class TimestampTzDomain(dbops.Domain):
    """Timestamptz clamped to years 0001-9999.

    The default timestamp range of (4713 BC - 294276 AD) has problems:
    Postgres isn't ISO compliant with years out of the 1-9999 range and
    language compatibility is questionable.
    """
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'timestamptz_t'),
            base='timestamptz',
            constraints=(
                dbops.DomainCheckConstraint(
                    domain_name=('edgedb', 'timestamptz_t'),
                    expr=("EXTRACT(years from VALUE) BETWEEN 1 AND 9999"),
                ),
            ),
        )


class TimestampDomain(dbops.Domain):
    """Timestamp clamped to years 0001-9999.

    The default timestamp range of (4713 BC - 294276 AD) has problems:
    Postgres isn't ISO compliant with years out of the 1-9999 range and
    language compatibility is questionable.
    """
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'timestamp_t'),
            base='timestamp',
            constraints=(
                dbops.DomainCheckConstraint(
                    domain_name=('edgedb', 'timestamp_t'),
                    expr=("EXTRACT(years from VALUE) BETWEEN 1 AND 9999"),
                ),
            ),
        )


class DateDomain(dbops.Domain):
    """Date clamped to years 0001-9999.

    The default timestamp range of (4713 BC - 294276 AD) has problems:
    Postgres isn't ISO compliant with years out of the 1-9999 range and
    language compatibility is questionable.
    """
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'date_t'),
            base='date',
            constraints=(
                dbops.DomainCheckConstraint(
                    domain_name=('edgedb', 'date_t'),
                    expr=("EXTRACT(years from VALUE) BETWEEN 1 AND 9999"),
                ),
            ),
        )


class DurationDomain(dbops.Domain):
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'duration_t'),
            base='interval',
            constraints=(
                dbops.DomainCheckConstraint(
                    domain_name=('edgedb', 'duration_t'),
                    expr=r'''
                        EXTRACT(months from VALUE) = 0 AND
                        EXTRACT(years from VALUE) = 0 AND
                        EXTRACT(days from VALUE) = 0
                    ''',
                ),
            ),
        )


class RelativeDurationDomain(dbops.Domain):
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'relative_duration_t'),
            base='interval',
            constraints=(
                dbops.DomainCheckConstraint(
                    domain_name=('edgedb', 'relative_duration_t'),
                    expr="true",
                ),
            ),
        )


class DateDurationDomain(dbops.Domain):
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'date_duration_t'),
            base='interval',
            constraints=(
                dbops.DomainCheckConstraint(
                    domain_name=('edgedb', 'date_duration_t'),
                    expr=r'''
                        EXTRACT(hour from VALUE) = 0 AND
                        EXTRACT(minute from VALUE) = 0 AND
                        EXTRACT(second from VALUE) = 0
                    ''',
                ),
            ),
        )


class Float32Range(dbops.Range):
    def __init__(self) -> None:
        super().__init__(
            name=types.type_to_range_name_map[('float4',)],
            subtype=('float4',),
        )


class Float64Range(dbops.Range):
    def __init__(self) -> None:
        super().__init__(
            name=types.type_to_range_name_map[('float8',)],
            subtype=('float8',),
            subtype_diff=('float8mi',)
        )


class DatetimeRange(dbops.Range):
    def __init__(self) -> None:
        super().__init__(
            name=types.type_to_range_name_map[('edgedb', 'timestamptz_t')],
            subtype=('edgedb', 'timestamptz_t'),
        )


class LocalDatetimeRange(dbops.Range):
    def __init__(self) -> None:
        super().__init__(
            name=types.type_to_range_name_map[('edgedb', 'timestamp_t')],
            subtype=('edgedb', 'timestamp_t'),
        )


class RangeToJsonFunction(dbops.Function):
    """Convert anyrange to a jsonb object."""
    text = r'''
        SELECT
            CASE
            WHEN val IS NULL THEN
                NULL
            WHEN isempty(val) THEN
                jsonb_build_object('empty', true)
            ELSE
                to_jsonb(o)
            END
        FROM
            (SELECT
                lower(val) as lower,
                lower_inc(val) as inc_lower,
                upper(val) as upper,
                upper_inc(val) as inc_upper
            ) AS o
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'range_to_jsonb'),
            args=[
                ('val', ('anyrange',)),
            ],
            returns=('jsonb',),
            volatility='immutable',
            language='sql',
            text=self.text,
        )


class MultiRangeToJsonFunction(dbops.Function):
    """Convert anymultirange to a jsonb object."""
    text = r'''
        SELECT
            CASE
            WHEN val IS NULL THEN
                NULL
            WHEN isempty(val) THEN
                jsonb_build_array()
            ELSE
                (
                    SELECT
                        jsonb_agg(edgedb.range_to_jsonb(m.el))
                    FROM
                        (SELECT
                            unnest(val) AS el
                        ) AS m
                )
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'multirange_to_jsonb'),
            args=[
                ('val', ('anymultirange',)),
            ],
            returns=('jsonb',),
            volatility='immutable',
            language='sql',
            text=self.text,
        )


class RangeValidateFunction(dbops.Function):
    """Range constructor validation function."""
    text = r'''
        SELECT
            CASE
            WHEN
                empty
                AND (lower IS DISTINCT FROM upper
                     OR lower IS NOT NULL AND inc_upper AND inc_lower)
            THEN
                edgedb.raise(
                    NULL::bool,
                    'invalid_parameter_value',
                    msg => 'conflicting arguments in range constructor:'
                           || ' "empty" is `true` while the specified'
                           || ' bounds suggest otherwise'
                )
            ELSE
                empty
            END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'range_validate'),
            args=[
                ('lower', ('anyelement',)),
                ('upper', ('anyelement',)),
                ('inc_lower', ('bool',)),
                ('inc_upper', ('bool',)),
                ('empty', ('bool',)),
            ],
            returns=('bool',),
            volatility='immutable',
            language='sql',
            text=self.text,
        )


class RangeUnpackLowerValidateFunction(dbops.Function):
    """Range unpack validation function."""
    text = r'''
        SELECT
            CASE WHEN
                NOT isempty(range)
            THEN
                edgedb.raise_on_null(
                    lower(range),
                    'invalid_parameter_value',
                    msg => 'cannot unpack an unbounded range'
                )
            ELSE
                lower(range)
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'range_lower_validate'),
            args=[
                ('range', ('anyrange',)),
            ],
            returns=('anyelement',),
            volatility='immutable',
            language='sql',
            text=self.text,
        )


class RangeUnpackUpperValidateFunction(dbops.Function):
    """Range unpack validation function."""
    text = r'''
        SELECT
            CASE WHEN
                NOT isempty(range)
            THEN
                edgedb.raise_on_null(
                    upper(range),
                    'invalid_parameter_value',
                    msg => 'cannot unpack an unbounded range'
                )
            ELSE
                upper(range)
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'range_upper_validate'),
            args=[
                ('range', ('anyrange',)),
            ],
            returns=('anyelement',),
            volatility='immutable',
            language='sql',
            text=self.text,
        )


class StrToConfigMemoryFunction(dbops.Function):
    """An implementation of std::str to cfg::memory cast."""
    text = r'''
        SELECT
            (CASE
                WHEN m.v[1] IS NOT NULL AND m.v[2] IS NOT NULL
                THEN (
                    CASE
                        WHEN m.v[2] = 'B'
                        THEN m.v[1]::int8

                        WHEN m.v[2] = 'KiB'
                        THEN m.v[1]::int8 * 1024

                        WHEN m.v[2] = 'MiB'
                        THEN m.v[1]::int8 * 1024 * 1024

                        WHEN m.v[2] = 'GiB'
                        THEN m.v[1]::int8 * 1024 * 1024 * 1024

                        WHEN m.v[2] = 'TiB'
                        THEN m.v[1]::int8 * 1024 * 1024 * 1024 * 1024

                        WHEN m.v[2] = 'PiB'
                        THEN m.v[1]::int8 * 1024 * 1024 * 1024 * 1024 * 1024

                        ELSE
                            -- Won't happen but we still have a guard for
                            -- completeness.
                            edgedb.raise(
                                NULL::int8,
                                'invalid_parameter_value',
                                msg => (
                                    'unsupported memory size unit "' ||
                                    m.v[2] || '"'
                                )
                            )
                    END
                )
                ELSE
                    CASE
                        WHEN "val" = '0'
                        THEN 0::int8
                        ELSE
                            edgedb.raise(
                                NULL::int8,
                                'invalid_parameter_value',
                                msg => (
                                    'unable to parse memory size "' ||
                                    "val" || '"'
                                )
                            )
                    END
            END)::edgedb.memory_t
        FROM LATERAL (
            SELECT regexp_match(
                "val", '^(\d+)([[:alpha:]]+)$') AS v
        ) AS m
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_cfg_memory'),
            args=[
                ('val', ('text',)),
            ],
            returns=('edgedb', 'memory_t'),
            strict=True,
            volatility='immutable',
            language='sql',
            text=self.text,
        )


class ConfigMemoryToStrFunction(dbops.Function):
    """An implementation of cfg::memory to std::str cast."""
    text = r'''
        SELECT
            CASE
                WHEN
                    "val" >= (1024::int8 * 1024 * 1024 * 1024 * 1024) AND
                    "val" % (1024::int8 * 1024 * 1024 * 1024 * 1024) = 0
                THEN
                    (
                        "val" / (1024::int8 * 1024 * 1024 * 1024 * 1024)
                    )::text || 'PiB'

                WHEN
                    "val" >= (1024::int8 * 1024 * 1024 * 1024) AND
                    "val" % (1024::int8 * 1024 * 1024 * 1024) = 0
                THEN
                    (
                        "val" / (1024::int8 * 1024 * 1024 * 1024)
                    )::text || 'TiB'

                WHEN
                    "val" >= (1024::int8 * 1024 * 1024) AND
                    "val" % (1024::int8 * 1024 * 1024) = 0
                THEN ("val" / (1024::int8 * 1024 * 1024))::text || 'GiB'

                WHEN "val" >= 1024::int8 * 1024 AND
                     "val" % (1024::int8 * 1024) = 0
                THEN ("val" / (1024::int8 * 1024))::text || 'MiB'

                WHEN "val" >= 1024 AND "val" % 1024 = 0
                THEN ("val" / 1024::int8)::text || 'KiB'

                ELSE "val"::text || 'B'
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'cfg_memory_to_str'),
            args=[
                ('val', ('edgedb', 'memory_t')),
            ],
            returns=('text',),
            volatility='immutable',
            language='sql',
            text=self.text,
        )


class AlterCurrentDatabaseSetString(dbops.Function):
    """Alter a PostgreSQL configuration parameter of the current database."""
    text = '''
    BEGIN
        EXECUTE 'ALTER DATABASE ' || quote_ident(current_database())
        || ' SET ' || quote_ident(parameter) || ' = '
        || coalesce(quote_literal(value), 'DEFAULT');
        RETURN value;
    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_alter_current_database_set'),
            args=[('parameter', ('text',)), ('value', ('text',))],
            returns=('text',),
            volatility='volatile',
            language='plpgsql',
            text=self.text,
        )


class AlterCurrentDatabaseSetStringArray(dbops.Function):
    """Alter a PostgreSQL configuration parameter of the current database."""
    text = '''
    BEGIN
        EXECUTE 'ALTER DATABASE ' || quote_ident(current_database())
        || ' SET ' || quote_ident(parameter) || ' = '
        || coalesce(
            (SELECT
                array_to_string(array_agg(quote_literal(q.v)), ',')
             FROM
                unnest(value) AS q(v)
            ),
            'DEFAULT'
        );
        RETURN value;
    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_alter_current_database_set'),
            args=[
                ('parameter', ('text',)),
                ('value', ('text[]',)),
            ],
            returns=('text[]',),
            volatility='volatile',
            language='plpgsql',
            text=self.text,
        )


class AlterCurrentDatabaseSetNonArray(dbops.Function):
    """Alter a PostgreSQL configuration parameter of the current database."""
    text = '''
    BEGIN
        EXECUTE 'ALTER DATABASE ' || quote_ident(current_database())
        || ' SET ' || quote_ident(parameter) || ' = '
        || coalesce(value::text, 'DEFAULT');
        RETURN value;
    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_alter_current_database_set'),
            args=[
                ('parameter', ('text',)),
                ('value', ('anynonarray',)),
            ],
            returns=('anynonarray',),
            volatility='volatile',
            language='plpgsql',
            text=self.text,
        )


class AlterCurrentDatabaseSetArray(dbops.Function):
    """Alter a PostgreSQL configuration parameter of the current database."""
    text = '''
    BEGIN
        EXECUTE 'ALTER DATABASE ' || quote_ident(current_database())
        || ' SET ' || quote_ident(parameter) || ' = '
        || coalesce(
            (SELECT
                array_to_string(array_agg(q.v::text), ',')
             FROM
                unnest(value) AS q(v)
            ),
            'DEFAULT'
        );
        RETURN value;
    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_alter_current_database_set'),
            args=[
                ('parameter', ('text',)),
                ('value', ('anyarray',)),
            ],
            returns=('anyarray',),
            volatility='volatile',
            language='plpgsql',
            text=self.text,
        )


class StrToBigint(dbops.Function):
    """Parse bigint from text."""

    # The plpgsql execption handling nonsense is actually just so that
    # we can produce an exception that mentions edgedb.bigint_t
    # instead of numeric, and thus produce the right user-facing
    # exception. As a nice side effect it is like twice as fast
    # as the previous code too.
    text = r'''
        DECLARE
            v numeric;
        BEGIN
            BEGIN
              v := val::numeric;
            EXCEPTION
              WHEN OTHERS THEN
                 v := NULL;
            END;

            IF scale(v) = 0 THEN
                RETURN v::edgedb.bigint_t;
            ELSE
                EXECUTE edgedb.raise(
                    NULL::numeric,
                    'invalid_text_representation',
                    msg => (
                        'invalid input syntax for type edgedb.bigint_t: '
                        || quote_literal(val)
                    )
                );
            END IF;
        END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_bigint'),
            args=[('val', ('text',))],
            returns=('edgedb', 'bigint_t'),
            language='plpgsql',
            volatility='immutable',
            strict=True,
            text=self.text)


class StrToDecimal(dbops.Function):
    """Parse decimal from text."""
    text = r'''
        SELECT
            (CASE WHEN v.column1 != 'NaN' THEN
                v.column1
            ELSE
                edgedb.raise(
                    NULL::numeric,
                    'invalid_text_representation',
                    msg => (
                        'invalid input syntax for type numeric: '
                        || quote_literal(val)
                    )
                )
            END)
        FROM
            (VALUES (
                val::numeric
            )) AS v
        ;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_decimal'),
            args=[('val', ('text',))],
            returns=('numeric',),
            volatility='immutable',
            strict=True,
            text=self.text,
        )


class StrToInt64NoInline(dbops.Function):
    """String-to-int64 cast with noinline guard.

    Adding a LIMIT clause to the function statement makes it
    uninlinable due to the Postgres inlining heuristic looking
    for simple SELECT expressions only (i.e. no clauses.)

    This might need to change in the future if the heuristic
    changes.
    """
    text = r'''
        SELECT
            "val"::bigint
        LIMIT
            1
        ;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_int64_noinline'),
            args=[('val', ('text',))],
            returns=('bigint',),
            volatility='immutable',
            text=self.text,
        )


class StrToInt32NoInline(dbops.Function):
    """String-to-int32 cast with noinline guard."""
    text = r'''
        SELECT
            "val"::int
        LIMIT
            1
        ;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_int32_noinline'),
            args=[('val', ('text',))],
            returns=('int',),
            volatility='immutable',
            text=self.text,
        )


class StrToInt16NoInline(dbops.Function):
    """String-to-int16 cast with noinline guard."""
    text = r'''
        SELECT
            "val"::smallint
        LIMIT
            1
        ;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_int16_noinline'),
            args=[('val', ('text',))],
            returns=('smallint',),
            volatility='immutable',
            text=self.text,
        )


class StrToFloat64NoInline(dbops.Function):
    """String-to-float64 cast with noinline guard."""
    text = r'''
        SELECT
            "val"::float8
        LIMIT
            1
        ;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_float64_noinline'),
            args=[('val', ('text',))],
            returns=('float8',),
            volatility='immutable',
            text=self.text,
        )


class StrToFloat32NoInline(dbops.Function):
    """String-to-float32 cast with noinline guard."""
    text = r'''
        SELECT
            "val"::float4
        LIMIT
            1
        ;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_float32_noinline'),
            args=[('val', ('text',))],
            returns=('float4',),
            volatility='immutable',
            text=self.text,
        )


class GetBackendCapabilitiesFunction(dbops.Function):

    text = f'''
        SELECT
            (json ->> 'capabilities')::bigint
        FROM
            edgedbinstdata.instdata
        WHERE
            key = 'backend_instance_params'
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_backend_capabilities'),
            args=[],
            returns=('bigint',),
            language='sql',
            volatility='stable',
            text=self.text,
        )


class GetBackendTenantIDFunction(dbops.Function):

    text = f'''
        SELECT
            (json ->> 'tenant_id')::text
        FROM
            edgedbinstdata.instdata
        WHERE
            key = 'backend_instance_params'
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_backend_tenant_id'),
            args=[],
            returns=('text',),
            language='sql',
            volatility='stable',
            text=self.text,
        )


class GetDatabaseBackendNameFunction(dbops.Function):

    text = f'''
    SELECT
        CASE
        WHEN
            (edgedb.get_backend_capabilities()
             & {int(params.BackendCapabilities.CREATE_DATABASE)}) != 0
        THEN
            edgedb.get_backend_tenant_id() || '_' || "db_name"
        ELSE
            current_database()::text
        END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_database_backend_name'),
            args=[('db_name', ('text',))],
            returns=('text',),
            language='sql',
            volatility='stable',
            text=self.text,
        )


class GetRoleBackendNameFunction(dbops.Function):

    text = f'''
    SELECT
        CASE
        WHEN
            (edgedb.get_backend_capabilities()
             & {int(params.BackendCapabilities.CREATE_ROLE)}) != 0
        THEN
            edgedb.get_backend_tenant_id() || '_' || "role_name"
        ELSE
            current_user::text
        END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_role_backend_name'),
            args=[('role_name', ('text',))],
            returns=('text',),
            language='sql',
            volatility='stable',
            text=self.text,
        )


class GetUserSequenceBackendNameFunction(dbops.Function):

    text = f"""
        SELECT
            'edgedbpub',
            "sequence_type_id"::text || '_sequence'
    """

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_user_sequence_backend_name'),
            args=[('sequence_type_id', ('uuid',))],
            returns=('record',),
            language='sql',
            volatility='stable',
            text=self.text,
        )


class GetSequenceBackendNameFunction(dbops.Function):

    text = f'''
        SELECT
            (CASE
                WHEN edgedb.get_name_module(st.name)
                     = any(edgedb.get_std_modules())
                THEN 'edgedbstd'
                ELSE 'edgedbpub'
             END),
            "sequence_type_id"::text || '_sequence'
        FROM
            edgedb."_SchemaScalarType" AS st
        WHERE
            st.id = "sequence_type_id"
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_sequence_backend_name'),
            args=[('sequence_type_id', ('uuid',))],
            returns=('record',),
            language='sql',
            volatility='stable',
            text=self.text,
        )


class GetStdModulesFunction(dbops.Function):

    text = f'''
        SELECT ARRAY[{",".join(ql(str(m)) for m in s_schema.STD_MODULES)}]
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_std_modules'),
            args=[],
            returns=('text[]',),
            language='sql',
            volatility='immutable',
            text=self.text,
        )


class GetObjectMetadata(dbops.Function):
    """Return EdgeDB metadata associated with a backend object."""
    text = '''
        SELECT
            CASE WHEN substr(d, 1, char_length({prefix})) = {prefix}
            THEN substr(d, char_length({prefix}) + 1)::jsonb
            ELSE '{{}}'::jsonb
            END
        FROM
            obj_description("objoid", "objclass") AS d
    '''.format(
        prefix=f'E{ql(defines.EDGEDB_VISIBLE_METADATA_PREFIX)}',
    )

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'obj_metadata'),
            args=[('objoid', ('oid',)), ('objclass', ('text',))],
            returns=('jsonb',),
            volatility='stable',
            text=self.text)


class GetColumnMetadata(dbops.Function):
    """Return EdgeDB metadata associated with a backend object."""
    text = '''
        SELECT
            CASE WHEN substr(d, 1, char_length({prefix})) = {prefix}
            THEN substr(d, char_length({prefix}) + 1)::jsonb
            ELSE '{{}}'::jsonb
            END
        FROM
            col_description("tableoid", "column") AS d
    '''.format(
        prefix=f'E{ql(defines.EDGEDB_VISIBLE_METADATA_PREFIX)}',
    )

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'col_metadata'),
            args=[('tableoid', ('oid',)), ('column', ('integer',))],
            returns=('jsonb',),
            volatility='stable',
            text=self.text)


class GetSharedObjectMetadata(dbops.Function):
    """Return EdgeDB metadata associated with a backend object."""
    text = '''
        SELECT
            CASE WHEN substr(d, 1, char_length({prefix})) = {prefix}
            THEN substr(d, char_length({prefix}) + 1)::jsonb
            ELSE '{{}}'::jsonb
            END
        FROM
            shobj_description("objoid", "objclass") AS d
    '''.format(
        prefix=f'E{ql(defines.EDGEDB_VISIBLE_METADATA_PREFIX)}',
    )

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'shobj_metadata'),
            args=[('objoid', ('oid',)), ('objclass', ('text',))],
            returns=('jsonb',),
            volatility='stable',
            text=self.text)


class GetDatabaseMetadataFunction(dbops.Function):
    """Return EdgeDB metadata associated with a given database."""
    text = f'''
        SELECT
            CASE
            WHEN
                "dbname" = {ql(defines.EDGEDB_SUPERUSER_DB)}
                OR (edgedb.get_backend_capabilities()
                    & {int(params.BackendCapabilities.CREATE_DATABASE)}) != 0
            THEN
                edgedb.shobj_metadata(
                    (SELECT
                        oid
                     FROM
                        pg_database
                     WHERE
                        datname = edgedb.get_database_backend_name("dbname")
                    ),
                    'pg_database'
                )
            ELSE
                COALESCE(
                    (SELECT
                        json
                     FROM
                        edgedbinstdata.instdata
                     WHERE
                        key = "dbname" || 'metadata'
                    ),
                    '{{}}'::jsonb
                )
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_database_metadata'),
            args=[('dbname', ('text',))],
            returns=('jsonb',),
            volatility='stable',
            text=self.text,
        )


class GetCurrentDatabaseFunction(dbops.Function):

    text = f'''
        SELECT
            CASE
            WHEN
                (edgedb.get_backend_capabilities()
                 & {int(params.BackendCapabilities.CREATE_DATABASE)}) != 0
            THEN
                substr(
                    current_database(),
                    char_length(edgedb.get_backend_tenant_id()) + 2
                )
            ELSE
                {ql(defines.EDGEDB_SUPERUSER_DB)}
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_current_database'),
            args=[],
            returns=('text',),
            language='sql',
            volatility='stable',
            text=self.text,
        )


class RaiseExceptionFunction(dbops.Function):
    text = '''
    BEGIN
        RAISE EXCEPTION USING
            ERRCODE = "exc",
            MESSAGE = "msg",
            DETAIL = COALESCE("detail", ''),
            HINT = COALESCE("hint", ''),
            COLUMN = COALESCE("column", ''),
            CONSTRAINT = COALESCE("constraint", ''),
            DATATYPE = COALESCE("datatype", ''),
            TABLE = COALESCE("table", ''),
            SCHEMA = COALESCE("schema", '');
        RETURN "rtype";
    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'raise'),
            args=[
                ('rtype', ('anyelement',)),
                ('exc', ('text',), "'raise_exception'"),
                ('msg', ('text',), "''"),
                ('detail', ('text',), "''"),
                ('hint', ('text',), "''"),
                ('column', ('text',), "''"),
                ('constraint', ('text',), "''"),
                ('datatype', ('text',), "''"),
                ('table', ('text',), "''"),
                ('schema', ('text',), "''"),
            ],
            returns=('anyelement',),
            # NOTE: The main reason why we don't want this function to be
            # immutable is that immutable functions can be
            # pre-evaluated by the query planner once if they have
            # constant arguments. This means that using this function
            # as the second argument in a COALESCE will raise an
            # exception regardless of whether the first argument is
            # NULL or not.
            volatility='stable',
            language='plpgsql',
            text=self.text,
        )


class RaiseExceptionOnNullFunction(dbops.Function):
    """Return the passed value or raise an exception if it's NULL."""
    text = '''
        SELECT coalesce(
            val,
            edgedb.raise(
                val,
                exc,
                msg => msg,
                detail => detail,
                hint => hint,
                "column" => "column",
                "constraint" => "constraint",
                "datatype" => "datatype",
                "table" => "table",
                "schema" => "schema"
            )
        )
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'raise_on_null'),
            args=[
                ('val', ('anyelement',)),
                ('exc', ('text',)),
                ('msg', ('text',)),
                ('detail', ('text',), "''"),
                ('hint', ('text',), "''"),
                ('column', ('text',), "''"),
                ('constraint', ('text',), "''"),
                ('datatype', ('text',), "''"),
                ('table', ('text',), "''"),
                ('schema', ('text',), "''"),
            ],
            returns=('anyelement',),
            # Same volatility as raise()
            volatility='stable',
            text=self.text,
        )


class RaiseExceptionOnNotNullFunction(dbops.Function):
    """Return the passed value or raise an exception if it's NOT NULL."""
    text = '''
        SELECT
            CASE
            WHEN val IS NULL THEN
                val
            ELSE
                edgedb.raise(
                    val,
                    exc,
                    msg => msg,
                    detail => detail,
                    hint => hint,
                    "column" => "column",
                    "constraint" => "constraint",
                    "datatype" => "datatype",
                    "table" => "table",
                    "schema" => "schema"
                )
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'raise_on_not_null'),
            args=[
                ('val', ('anyelement',)),
                ('exc', ('text',)),
                ('msg', ('text',)),
                ('detail', ('text',), "''"),
                ('hint', ('text',), "''"),
                ('column', ('text',), "''"),
                ('constraint', ('text',), "''"),
                ('datatype', ('text',), "''"),
                ('table', ('text',), "''"),
                ('schema', ('text',), "''"),
            ],
            returns=('anyelement',),
            # Same volatility as raise()
            volatility='stable',
            text=self.text,
        )


class RaiseExceptionOnEmptyStringFunction(dbops.Function):
    """Return the passed string or raise an exception if it's empty."""
    text = '''
        SELECT
            CASE WHEN edgedb._length(val) = 0 THEN
                edgedb.raise(val, exc, msg => msg, detail => detail)
            ELSE
                val
            END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'raise_on_empty'),
            args=[
                ('val', ('anyelement',)),
                ('exc', ('text',)),
                ('msg', ('text',)),
                ('detail', ('text',), "''"),
            ],
            returns=('anyelement',),
            # Same volatility as raise()
            volatility='stable',
            text=self.text,
        )


class AssertJSONTypeFunction(dbops.Function):
    """Assert that the JSON type matches what is expected."""
    text = '''
        SELECT
            CASE WHEN array_position(typenames, jsonb_typeof(val)) IS NULL THEN
                edgedb.raise(
                    NULL::jsonb,
                    'wrong_object_type',
                    msg => coalesce(
                        msg,
                        (
                            'expected JSON '
                            || array_to_string(typenames, ' or ')
                            || '; got JSON '
                            || coalesce(jsonb_typeof(val), 'UNKNOWN')
                        )
                    ),
                    detail => detail
                )
            ELSE
                val
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'jsonb_assert_type'),
            args=[
                ('val', ('jsonb',)),
                ('typenames', ('text[]',)),
                ('msg', ('text',), 'NULL'),
                ('detail', ('text',), "''"),
            ],
            returns=('jsonb',),
            # Max volatility of raise() and array_to_string() (stable)
            volatility='stable',
            text=self.text,
        )


class ExtractJSONScalarFunction(dbops.Function):
    """Convert a given JSON scalar value into a text value."""
    text = '''
        SELECT
            (to_jsonb(ARRAY[
                edgedb.jsonb_assert_type(
                    coalesce(val, 'null'::jsonb),
                    ARRAY[json_typename, 'null'],
                    msg => msg,
                    detail => detail
                )
            ])->>0)
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'jsonb_extract_scalar'),
            args=[
                ('val', ('jsonb',)),
                ('json_typename', ('text',)),
                ('msg', ('text',), 'NULL'),
                ('detail', ('text',), "''"),
            ],
            returns=('text',),
            volatility='immutable',
            text=self.text,
        )


class GetSchemaObjectNameFunction(dbops.Function):
    text = '''
        SELECT coalesce(
            (SELECT name FROM edgedb."_SchemaObject"
             WHERE id = type::uuid),
            edgedb.raise(
                NULL::text,
                msg => 'resolve_type_name: unknown type: "' || type || '"'
            )
        )
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_get_schema_object_name'),
            args=[('type', ('uuid',))],
            returns=('text',),
            # Max volatility of raise() and a SELECT from a
            # table (stable).
            volatility='stable',
            text=self.text,
            strict=True,
        )


class IssubclassFunction(dbops.Function):
    text = '''
        SELECT
            clsid = any(classes) OR (
                SELECT classes && q.ancestors
                FROM
                    (SELECT
                        array_agg(o.target) AS ancestors
                        FROM edgedb."_SchemaInheritingObject__ancestors" o
                        WHERE o.source = clsid
                    ) AS q
            );
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'issubclass'),
            args=[('clsid', 'uuid'), ('classes', 'uuid[]')],
            returns='bool',
            volatility='stable',
            text=self.__class__.text)


class IssubclassFunction2(dbops.Function):
    text = '''
        SELECT
            clsid = pclsid OR (
                SELECT
                    pclsid IN (
                        SELECT
                            o.target
                        FROM edgedb."_SchemaInheritingObject__ancestors" o
                            WHERE o.source = clsid
                    )
            );
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'issubclass'),
            args=[('clsid', 'uuid'), ('pclsid', 'uuid')],
            returns='bool',
            volatility='stable',
            text=self.__class__.text)


class NormalizeNameFunction(dbops.Function):
    text = '''
        SELECT
            CASE WHEN strpos(name, '@') = 0 THEN
                name
            ELSE
                CASE WHEN strpos(name, '::') = 0 THEN
                    replace(split_part(name, '@', 1), '|', '::')
                ELSE
                    replace(
                        split_part(
                            -- "reverse" calls are to emulate "rsplit"
                            reverse(split_part(reverse(name), '::', 1)),
                            '@', 1),
                        '|', '::')
                END
            END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'shortname_from_fullname'),
            args=[('name', 'text')],
            returns='text',
            volatility='immutable',
            language='sql',
            text=self.__class__.text)


class GetNameModuleFunction(dbops.Function):
    text = '''
        SELECT reverse(split_part(reverse("name"), '::', 1))
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_name_module'),
            args=[('name', 'text')],
            returns='text',
            volatility='immutable',
            language='sql',
            text=self.__class__.text)


class NullIfArrayNullsFunction(dbops.Function):
    """Check if array contains NULLs and if so, return NULL."""
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_nullif_array_nulls'),
            args=[('a', 'anyarray')],
            returns='anyarray',
            volatility='stable',
            language='sql',
            text='''
                SELECT CASE WHEN array_position(a, NULL) IS NULL
                THEN a ELSE NULL END
            ''')


class NormalizeArrayIndexFunction(dbops.Function):
    """Convert an EdgeQL index to SQL index."""

    text = '''
        SELECT
            CASE WHEN index > (2147483647-1) OR index < -2147483648 THEN
                NULL
            WHEN index < 0 THEN
                length + index::int + 1
            ELSE
                index::int + 1
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_normalize_array_index'),
            args=[('index', ('bigint',)), ('length', ('int',))],
            returns=('int',),
            volatility='immutable',
            text=self.text,
        )


class NormalizeArraySliceIndexFunction(dbops.Function):
    """Convert an EdgeQL index to SQL index (for slices)"""

    text = '''
        SELECT
            GREATEST(0, LEAST(2147483647,
                CASE WHEN index < 0 THEN
                    length::bigint + index + 1
                ELSE
                    index + 1
                END
            ))
        WHERE index IS NOT NULL
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_normalize_array_slice_index'),
            args=[('index', ('bigint',)), ('length', ('int',))],
            returns=('int',),
            volatility='immutable',
            text=self.text,
        )


class IntOrNullFunction(dbops.Function):
    """
    Convert bigint to int. If it does not fit, return NULL.
    """

    text = """
        SELECT
            CASE WHEN val <= 2147483647 AND val >= -2147483648 THEN
                val
            ELSE
                NULL
            END
    """

    def __init__(self) -> None:
        super().__init__(
            name=("edgedb", "_int_or_null"),
            args=[("val", ("bigint",))],
            returns=("int",),
            volatility="immutable",
            strict=True,
            text=self.text,
        )


class ArrayIndexWithBoundsFunction(dbops.Function):
    """Get an array element or raise an out-of-bounds exception."""

    text = '''
        SELECT CASE WHEN val IS NULL THEN
            NULL
        ELSE
            edgedb.raise_on_null(
                val[edgedb._normalize_array_index(index, array_upper(val, 1))],
                'array_subscript_error',
                msg => 'array index ' || index::text || ' is out of bounds',
                detail => detail
            )
        END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_index'),
            args=[('val', ('anyarray',)), ('index', ('bigint',)),
                  ('detail', ('text',))],
            returns=('anyelement',),
            # Min volatility of exception helpers and pg_typeof is 'stable',
            # but for all practical purposes, we can assume 'immutable'
            volatility='immutable',
            text=self.text,
        )


class ArraySliceFunction(dbops.Function):
    """Get an array slice."""

    # This function is also inlined in expr.py#_inline_array_slicing.

    # Known bug: if array has 2G elements and both bounds are overflowing,
    # this will return last element instead of an empty array.
    text = """
        SELECT val[
            edgedb._normalize_array_slice_index(start, cardinality(val))
            :
            edgedb._normalize_array_slice_index(stop, cardinality(val)) - 1
        ]
    """

    def __init__(self) -> None:
        super().__init__(
            name=("edgedb", "_slice"),
            args=[
                ("val", ("anyarray",)),
                ("start", ("bigint",)),
                ("stop", ("bigint",)),
            ],
            returns=("anyarray",),
            volatility="immutable",
            text=self.text,
        )


class StringIndexWithBoundsFunction(dbops.Function):
    """Get a string character or raise an out-of-bounds exception."""

    text = '''
        SELECT edgedb.raise_on_empty(
            CASE WHEN pg_index IS NULL THEN
                ''
            ELSE
                substr("val", pg_index, 1)
            END,
            'invalid_parameter_value',
            "typename" || ' index ' || "index"::text || ' is out of bounds',
            "detail"
        )
        FROM (
            SELECT (
                edgedb._normalize_array_index("index", char_length("val"))
            ) as pg_index
        ) t
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_index'),
            args=[
                ('val', ('text',)),
                ('index', ('bigint',)),
                ('detail', ('text',)),
                ('typename', ('text',), "'string'"),
            ],
            returns=('text',),
            # Min volatility of exception helpers and pg_typeof is 'stable',
            # but for all practical purposes, we can assume 'immutable'
            volatility='immutable',
            text=self.text,
        )


class BytesIndexWithBoundsFunction(dbops.Function):
    """Get a bytes character or raise an out-of-bounds exception."""

    text = '''
        SELECT edgedb.raise_on_empty(
            CASE WHEN pg_index IS NULL THEN
                ''::bytea
            ELSE
                substr("val", pg_index, 1)
            END,
            'invalid_parameter_value',
            'byte string index ' || "index"::text || ' is out of bounds',
            "detail"
        )
        FROM (
            SELECT (
                edgedb._normalize_array_index("index", length("val"))
            ) as pg_index
        ) t
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_index'),
            args=[
                ('val', ('bytea',)),
                ('index', ('bigint',)),
                ('detail', ('text',)),
            ],
            returns=('bytea',),
            # Min volatility of exception helpers and pg_typeof is 'stable',
            # but for all practical purposes, we can assume 'immutable'
            volatility='immutable',
            text=self.text,
        )


class SubstrProxyFunction(dbops.Function):
    """Same as substr, but interpret negative length as 0 instead."""

    text = r"""
        SELECT
            CASE
                WHEN length < 0 THEN ''
                ELSE substr(val, start::int, length)
            END
    """

    def __init__(self) -> None:
        super().__init__(
            name=("edgedb", "_substr"),
            args=[
                ("val", ("anyelement",)),
                ("start", ("int",)),
                ("length", ("int",)),
            ],
            returns=("anyelement",),
            volatility="immutable",
            strict=True,
            text=self.text,
        )


class LengthStringProxyFunction(dbops.Function):
    """Same as substr, but interpret negative length as 0 instead."""
    text = r'''
        SELECT char_length(val)
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_length'),
            args=[('val', ('text',))],
            returns=('int',),
            volatility='immutable',
            strict=True,
            text=self.text)


class LengthBytesProxyFunction(dbops.Function):
    """Same as substr, but interpret negative length as 0 instead."""
    text = r'''
        SELECT length(val)
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_length'),
            args=[('val', ('bytea',))],
            returns=('int',),
            volatility='immutable',
            strict=True,
            text=self.text)


class StringSliceImplFunction(dbops.Function):
    """Get a string slice."""

    text = r"""
        SELECT
            edgedb._substr(
                val,
                pg_start,
                pg_end - pg_start
            )
        FROM (SELECT
            edgedb._normalize_array_slice_index(
                start, edgedb._length(val)
            ) as pg_start,
            edgedb._normalize_array_slice_index(
                stop, edgedb._length(val)
            ) as pg_end
        ) t
    """

    def __init__(self) -> None:
        super().__init__(
            name=("edgedb", "_str_slice"),
            args=[
                ("val", ("anyelement",)),
                ("start", ("bigint",)),
                ("stop", ("bigint",)),
            ],
            returns=("anyelement",),
            volatility="immutable",
            text=self.text,
        )


class StringSliceFunction(dbops.Function):
    """Get a string slice."""
    text = r'''
        SELECT edgedb._str_slice(val, start, stop)
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_slice'),
            args=[
                ('val', ('text',)),
                ('start', ('bigint',)),
                ('stop', ('bigint',)),
            ],
            returns=('text',),
            volatility='immutable',
            text=self.text)


class BytesSliceFunction(dbops.Function):
    """Get a string slice."""
    text = r'''
        SELECT edgedb._str_slice(val, start, stop)
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_slice'),
            args=[
                ('val', ('bytea',)),
                ('start', ('bigint',)),
                ('stop', ('bigint',)),
            ],
            returns=('bytea',),
            volatility='immutable',
            text=self.text)


class JSONIndexByTextFunction(dbops.Function):
    """Get a JSON element by text index or raise an exception."""
    text = r'''
        SELECT
            CASE jsonb_typeof(val)
            WHEN 'object' THEN (
                edgedb.raise_on_null(
                    val -> index,
                    'invalid_parameter_value',
                    msg => (
                        'JSON index ' || quote_literal(index)
                        || ' is out of bounds'
                    ),
                    detail => detail
                )
            )
            WHEN 'array' THEN (
                edgedb.raise(
                    NULL::jsonb,
                    'wrong_object_type',
                    msg => (
                        'cannot index JSON ' || jsonb_typeof(val)
                        || ' by ' || pg_typeof(index)::text
                    ),
                    detail => detail
                )
            )
            ELSE
                edgedb.raise(
                    NULL::jsonb,
                    'wrong_object_type',
                    msg => (
                        'cannot index JSON '
                        || coalesce(jsonb_typeof(val), 'UNKNOWN')
                    ),
                    detail => (
                        '{"hint":"Retrieving an element by a string index '
                        || 'is only available for JSON objects."}'
                    )
                )
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_index'),
            args=[
                ('val', ('jsonb',)),
                ('index', ('text',)),
                ('detail', ('text',), "''"),
            ],
            returns=('jsonb',),
            # Min volatility of exception helpers 'stable',
            # but for all practical purposes, we can assume 'immutable'
            volatility='immutable',
            strict=True,
            text=self.text,
        )


class JSONIndexByIntFunction(dbops.Function):
    """Get a JSON element by int index or raise an exception."""

    text = r'''
        SELECT
            CASE jsonb_typeof(val)
            WHEN 'object' THEN (
                edgedb.raise(
                    NULL::jsonb,
                    'wrong_object_type',
                    msg => (
                        'cannot index JSON ' || jsonb_typeof(val)
                        || ' by ' || pg_typeof(index)::text
                    ),
                    detail => detail
                )
            )
            WHEN 'array' THEN (
                edgedb.raise_on_null(
                    val -> edgedb._int_or_null(index),
                    'invalid_parameter_value',
                    msg => 'JSON index ' || index::text || ' is out of bounds',
                    detail => detail
                )
            )
            WHEN 'string' THEN (
                to_jsonb(edgedb._index(
                    val#>>'{}',
                    index,
                    detail,
                    'JSON'
                ))
            )
            ELSE
                edgedb.raise(
                    NULL::jsonb,
                    'wrong_object_type',
                    msg => (
                        'cannot index JSON '
                        || coalesce(jsonb_typeof(val), 'UNKNOWN')
                    ),
                    detail => (
                        '{"hint":"Retrieving an element by an integer index '
                        || 'is only available for JSON arrays and strings."}'
                    )
                )
            END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_index'),
            args=[
                ('val', ('jsonb',)),
                ('index', ('bigint',)),
                ('detail', ('text',), "''"),
            ],
            returns=('jsonb',),
            # Min volatility of exception helpers and pg_typeof is 'stable',
            # but for all practical purposes, we can assume 'immutable'
            volatility='immutable',
            strict=True,
            text=self.text,
        )


class JSONSliceFunction(dbops.Function):
    """Get a JSON array slice."""

    text = r"""
        SELECT
            CASE
            WHEN val IS NULL THEN NULL
            WHEN jsonb_typeof(val) = 'array' THEN (
                to_jsonb(edgedb._slice(
                    (
                        SELECT coalesce(array_agg(value), '{}'::jsonb[])
                        FROM jsonb_array_elements(val)
                    ),
                    start, stop
                ))
            )
            WHEN jsonb_typeof(val) = 'string' THEN (
                to_jsonb(edgedb._slice(val#>>'{}', start, stop))
            )
            ELSE
                edgedb.raise(
                    NULL::jsonb,
                    'wrong_object_type',
                    msg => (
                        'cannot slice JSON '
                        || coalesce(jsonb_typeof(val), 'UNKNOWN')
                    ),
                    detail => (
                        '{"hint":"Slicing is only available for JSON arrays'
                        || ' and strings."}'
                    )
                )
            END
    """

    def __init__(self) -> None:
        super().__init__(
            name=("edgedb", "_slice"),
            args=[
                ("val", ("jsonb",)),
                ("start", ("bigint",)),
                ("stop", ("bigint",)),
            ],
            returns=("jsonb",),
            # Min volatility of to_jsonb is 'stable',
            # but for all practical purposes, we can assume 'immutable'
            volatility="immutable",
            text=self.text,
        )


# We need custom casting functions for various datetime scalars in
# order to enforce correctness w.r.t. local vs time-zone-aware
# datetime. Postgres does a lot of magic and guessing for time zones
# and generally will accept text with or without time zone for any
# particular flavor of timestamp. In order to guarantee that we can
# detect time-zones we restrict the inputs to ISO8601 format.
#
# See issue #740.
class DatetimeInFunction(dbops.Function):
    """Cast text into timestamptz using ISO8601 spec."""
    text = r'''
        SELECT
            CASE WHEN val !~ (
                    '^\s*(' ||
                        '(\d{4}-\d{2}-\d{2}|\d{8})' ||
                        '[ tT]' ||
                        '(\d{2}(:\d{2}(:\d{2}(\.\d+)?)?)?|\d{2,6}(\.\d+)?)' ||
                        '([zZ]|[-+](\d{2,4}|\d{2}:\d{2}))' ||
                    ')\s*$'
                )
            THEN
                edgedb.raise(
                    NULL::edgedb.timestamptz_t,
                    'invalid_datetime_format',
                    msg => (
                        'invalid input syntax for type timestamptz: '
                        || quote_literal(val)
                    ),
                    detail => (
                        '{"hint":"Please use ISO8601 format. Example: '
                        || '2010-12-27T23:59:59-07:00. Alternatively '
                        || '\"to_datetime\" function provides custom '
                        || 'formatting options."}'
                    )
                )
            ELSE
                val::edgedb.timestamptz_t
            END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'datetime_in'),
            args=[('val', ('text',))],
            returns=('edgedb', 'timestamptz_t'),
            # Same volatility as raise() (stable)
            volatility='stable',
            text=self.text)


class DurationInFunction(dbops.Function):
    """Cast text into duration, ensuring there is no days or months units"""
    text = r'''
        SELECT
            CASE WHEN
                EXTRACT(MONTH FROM v.column1) != 0 OR
                EXTRACT(YEAR FROM v.column1) != 0 OR
                EXTRACT(DAY FROM v.column1) != 0
            THEN
                edgedb.raise(
                    NULL::edgedb.duration_t,
                    'invalid_datetime_format',
                    msg => (
                        'invalid input syntax for type std::duration: '
                        || quote_literal(val)
                    ),
                    detail => (
                        '{"hint":"Day, month and year units cannot be used '
                        || 'for std::duration."}'
                    )
                )
            ELSE v.column1::edgedb.duration_t
            END
        FROM
            (VALUES (
                val::interval
            )) AS v
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'duration_in'),
            args=[('val', ('text',))],
            returns=('edgedb', 'duration_t'),
            volatility='immutable',
            text=self.text,
        )


class DateDurationInFunction(dbops.Function):
    """
    Cast text into date_duration, ensuring there is no unit smaller
    than days.
    """
    text = r'''
        SELECT
            CASE WHEN
                EXTRACT(HOUR FROM v.column1) != 0 OR
                EXTRACT(MINUTE FROM v.column1) != 0 OR
                EXTRACT(SECOND FROM v.column1) != 0
            THEN
                edgedb.raise(
                    NULL::edgedb.date_duration_t,
                    'invalid_datetime_format',
                    msg => (
                        'invalid input syntax for type cal::date_duration: '
                        || quote_literal(val)
                    ),
                    detail => (
                        '{"hint":"Units smaller than days cannot be used '
                        || 'for cal::date_duration."}'
                    )
                )
            ELSE v.column1::edgedb.date_duration_t
            END
        FROM
            (VALUES (
                val::interval
            )) AS v
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'date_duration_in'),
            args=[('val', ('text',))],
            returns=('edgedb', 'date_duration_t'),
            volatility='immutable',
            text=self.text,
        )


class LocalDatetimeInFunction(dbops.Function):
    """Cast text into timestamp using ISO8601 spec."""
    text = r'''
        SELECT
            CASE WHEN
                val !~ (
                    '^\s*(' ||
                        '(\d{4}-\d{2}-\d{2}|\d{8})' ||
                        '[ tT]' ||
                        '(\d{2}(:\d{2}(:\d{2}(\.\d+)?)?)?|\d{2,6}(\.\d+)?)' ||
                    ')\s*$'
                )
            THEN
                edgedb.raise(
                    NULL::edgedb.timestamp_t,
                    'invalid_datetime_format',
                    msg => (
                        'invalid input syntax for type timestamp: '
                        || quote_literal(val)
                    ),
                    detail => (
                        '{"hint":"Please use ISO8601 format. Example '
                        || '2010-04-18T09:27:00 Alternatively '
                        || '\"to_local_datetime\" function provides custom '
                        || 'formatting options."}'
                    )
                )
            ELSE
                val::edgedb.timestamp_t
            END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'local_datetime_in'),
            args=[('val', ('text',))],
            returns=('edgedb', 'timestamp_t'),
            volatility='immutable',
            text=self.text)


class LocalDateInFunction(dbops.Function):
    """Cast text into date using ISO8601 spec."""
    text = r'''
        SELECT
            CASE WHEN
                val !~ (
                    '^\s*(' ||
                        '(\d{4}-\d{2}-\d{2}|\d{8})' ||
                    ')\s*$'
                )
            THEN
                edgedb.raise(
                    NULL::edgedb.date_t,
                    'invalid_datetime_format',
                    msg => (
                        'invalid input syntax for type date: '
                        || quote_literal(val)
                    ),
                    detail => (
                        '{"hint":"Please use ISO8601 format. Example '
                        || '2010-04-18 Alternatively '
                        || '\"to_local_date\" function provides custom '
                        || 'formatting options."}'
                    )
                )
            ELSE
                val::edgedb.date_t
            END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'local_date_in'),
            args=[('val', ('text',))],
            returns=('edgedb', 'date_t'),
            volatility='immutable',
            text=self.text)


class LocalTimeInFunction(dbops.Function):
    """Cast text into time using ISO8601 spec."""
    text = r'''
        SELECT
            CASE WHEN date_part('hour', x.t) = 24
            THEN
                edgedb.raise(
                    NULL::time,
                    'invalid_datetime_format',
                    msg => (
                        'cal::local_time field value out of range: '
                        || quote_literal(val)
                    )
                )
            ELSE
                x.t
            END
        FROM (
            SELECT
                CASE WHEN val !~ ('^\s*(' ||
                        '(\d{2}(:\d{2}(:\d{2}(\.\d+)?)?)?|\d{2,6}(\.\d+)?)' ||
                    ')\s*$')
                THEN
                    edgedb.raise(
                        NULL::time,
                        'invalid_datetime_format',
                        msg => (
                            'invalid input syntax for type time: '
                            || quote_literal(val)
                        ),
                        detail => (
                            '{"hint":"Please use ISO8601 format. Examples: '
                            || '18:43:27 or 18:43 Alternatively '
                            || '\"to_local_time\" function provides custom '
                            || 'formatting options."}'
                        )
                    )
                ELSE
                    val::time
                END as t
        ) as x;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'local_time_in'),
            args=[('val', ('text',))],
            returns=('time',),
            volatility='immutable',
            text=self.text,
        )


class ToTimestampTZCheck(dbops.Function):
    """Checks if the original text has time zone or not."""
    # What are we trying to mitigate?
    # We're trying to detect that when we're casting to datetime the
    # time zone is in fact present in the input. It is a problem if
    # it's not since then one gets assigned implicitly based on the
    # server settings.
    #
    # It is insufficient to rely on the presence of TZH in the format
    # string, since `to_timestamp` will happily ignore the missing
    # time-zone in the input anyway. So in order to tell whether the
    # input string contained a time zone that was in fact parsed we
    # employ the following trick:
    #
    # If the time zone is in the input then it is unambiguous and the
    # parsed value will not depend on the current server time zone.
    # However, if the time zone was omitted, then the parsed value
    # will default to the server time zone. This implies that if
    # changing the server time zone for the same input string affects
    # the parsed value, the input string itself didn't contain a time
    # zone.
    text = r'''
        DECLARE
            result timestamptz;
            chk timestamptz;
            msg text;
        BEGIN
            result := to_timestamp(val, fmt);
            PERFORM set_config('TimeZone', 'America/Toronto', true);
            chk := to_timestamp(val, fmt);
            -- We're deliberately not doing any save/restore because
            -- the server MUST be in UTC. In fact, this check relies
            -- on it.
            PERFORM set_config('TimeZone', 'UTC', true);

            IF hastz THEN
                msg := 'missing required';
            ELSE
                msg := 'unexpected';
            END IF;

            IF (result = chk) != hastz THEN
                RAISE EXCEPTION USING
                    ERRCODE = 'invalid_datetime_format',
                    MESSAGE = msg || ' time zone in input ' ||
                        quote_literal(val),
                    DETAIL = '';
            END IF;

            RETURN result::edgedb.timestamptz_t;
        END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_to_timestamptz_check'),
            args=[('val', ('text',)), ('fmt', ('text',)),
                  ('hastz', ('bool',))],
            returns=('edgedb', 'timestamptz_t'),
            # We're relying on changing settings, so it's volatile.
            volatility='volatile',
            language='plpgsql',
            text=self.text)


class ToDatetimeFunction(dbops.Function):
    """Convert text into timestamptz using a formatting spec."""
    # NOTE that if only the TZM (minutes) are mentioned it is not
    # enough for a valid time zone definition
    text = r'''
        SELECT
            CASE WHEN fmt !~ (
                    '^(' ||
                        '("([^"\\]|\\.)*")|' ||
                        '([^"]+)' ||
                    ')*(TZH).*$'
                )
            THEN
                edgedb.raise(
                    NULL::edgedb.timestamptz_t,
                    'invalid_datetime_format',
                    msg => (
                        'missing required time zone in format: '
                        || quote_literal(fmt)
                    ),
                    detail => (
                        $h${"hint":"Use one or both of the following: $h$
                        || $h$'TZH', 'TZM'"}$h$
                    )
                )
            ELSE
                edgedb._to_timestamptz_check(val, fmt, true)
            END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'to_datetime'),
            args=[('val', ('text',)), ('fmt', ('text',))],
            returns=('edgedb', 'timestamptz_t'),
            # Same as _to_timestamptz_check.
            volatility='volatile',
            text=self.text)


class ToLocalDatetimeFunction(dbops.Function):
    """Convert text into timestamp using a formatting spec."""
    # NOTE time zone should not be mentioned at all.
    text = r'''
        SELECT
            CASE WHEN fmt ~ (
                    '^(' ||
                        '("([^"\\]|\\.)*")|' ||
                        '([^"]+)' ||
                    ')*(TZH|TZM).*$'
                )
            THEN
                edgedb.raise(
                    NULL::edgedb.timestamp_t,
                    'invalid_datetime_format',
                    msg => (
                        'unexpected time zone in format: '
                        || quote_literal(fmt)
                    )
                )
            ELSE
                edgedb._to_timestamptz_check(val, fmt, false)
                    ::edgedb.timestamp_t
            END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'to_local_datetime'),
            args=[('val', ('text',)), ('fmt', ('text',))],
            returns=('edgedb', 'timestamp_t'),
            # Same as _to_timestamptz_check.
            volatility='volatile',
            text=self.text)


class StrToBool(dbops.Function):
    """Parse bool from text."""
    # We first try to match case-insensitive "true|false" at all. On
    # null, we raise an exception. But otherwise we know that we have
    # an array of matches. The first element matching "true" and
    # second - "false". So the boolean value is then "true" if the
    # second array element is NULL and false otherwise.
    text = r'''
        SELECT (
            coalesce(
                regexp_match(val, '^\s*(?:(true)|(false))\s*$', 'i')::text[],
                edgedb.raise(
                    NULL::text[],
                    'invalid_text_representation',
                    msg => 'invalid input syntax for type bool: '
                           || quote_literal(val)
                )
            )
        )[2] IS NULL;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'str_to_bool'),
            args=[('val', ('text',))],
            returns=('bool',),
            strict=True,
            # Stable because it's raising exceptions.
            volatility='stable',
            text=self.text)


class QuoteLiteralFunction(dbops.Function):
    """Encode string as edgeql literal quoted string"""
    text = r'''
        SELECT concat('\'',
            replace(
                replace(val, '\\', '\\\\'),
                '\'', '\\\''),
            '\'')
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'quote_literal'),
            args=[('val', ('text',))],
            returns=('str',),
            volatility='immutable',
            text=self.text)


class QuoteIdentFunction(dbops.Function):
    """Quote ident function."""
    # TODO do not quote valid identifiers unless they are reserved
    text = r'''
        SELECT concat('`', replace(val, '`', '``'), '`')
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'quote_ident'),
            args=[('val', ('text',))],
            returns=('text',),
            volatility='immutable',
            text=self.text,
        )


class QuoteNameFunction(dbops.Function):

    text = r"""
        SELECT
            string_agg(edgedb.quote_ident(np), '::')
        FROM
            unnest(string_to_array("name", '::')) AS np
    """

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'quote_name'),
            args=[('name', ('text',))],
            returns=('text',),
            volatility='immutable',
            text=self.text,
        )


class DescribeRolesAsDDLFunctionForwardDecl(dbops.Function):
    """Forward declaration for _describe_roles_as_ddl"""

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_describe_roles_as_ddl'),
            args=[],
            returns=('text'),
            # Stable because it's raising exceptions.
            volatility='stable',
            text='SELECT NULL::text',
        )


class DescribeRolesAsDDLFunction(dbops.Function):
    """Describe roles as DDL"""

    def __init__(self, schema: s_schema.Schema) -> None:
        role_obj = schema.get("sys::Role", type=s_objtypes.ObjectType)
        roles = inhviewname(schema, role_obj)
        member_of = role_obj.getptr(schema, s_name.UnqualName('member_of'))
        members = inhviewname(schema, member_of)
        name_col = ptr_col_name(schema, role_obj, 'name')
        pass_col = ptr_col_name(schema, role_obj, 'password')
        qi_superuser = qlquote.quote_ident(defines.EDGEDB_SUPERUSER)
        text = f"""
            WITH RECURSIVE
            dependencies AS (
                SELECT r.id AS id, m.target AS parent
                    FROM {q(*roles)} r
                        LEFT OUTER JOIN {q(*members)} m ON r.id = m.source
            ),
            roles_with_depths(id, depth) AS (
                SELECT id, 0 FROM dependencies WHERE parent IS NULL
                UNION ALL
                SELECT dependencies.id, roles_with_depths.depth + 1
                FROM dependencies
                INNER JOIN roles_with_depths
                    ON dependencies.parent = roles_with_depths.id
            ),
            ordered_roles AS (
                SELECT id, max(depth) FROM roles_with_depths
                GROUP BY id
                ORDER BY max(depth) ASC
            )
            SELECT
            coalesce(string_agg(
                CASE WHEN
                    role.{qi(name_col)} = { ql(defines.EDGEDB_SUPERUSER) } THEN
                    NULLIF(concat(
                        'ALTER ROLE { qi_superuser } {{',
                        NULLIF((SELECT
                            concat(
                                ' EXTENDING ',
                                string_agg(
                                    edgedb.quote_ident(parent.{qi(name_col)}),
                                    ', '
                                ),
                                ';'
                            )
                            FROM {q(*members)} member
                                INNER JOIN {q(*roles)} parent
                                ON parent.id = member.target
                            WHERE member.source = role.id
                        ), ' EXTENDING ;'),
                        CASE WHEN role.{qi(pass_col)} IS NOT NULL THEN
                            concat(' SET password_hash := ',
                                   quote_literal(role.{qi(pass_col)}),
                                   ';')
                        ELSE '' END,
                        '}};'
                    ), 'ALTER ROLE { qi_superuser } {{}};')
                ELSE
                    concat(
                        'CREATE SUPERUSER ROLE ',
                        edgedb.quote_ident(role.{qi(name_col)}),
                        NULLIF((SELECT
                            concat(' EXTENDING ',
                                string_agg(
                                    edgedb.quote_ident(parent.{qi(name_col)}),
                                    ', '
                                )
                            )
                            FROM {q(*members)} member
                                INNER JOIN {q(*roles)} parent
                                ON parent.id = member.target
                            WHERE member.source = role.id
                        ), ' EXTENDING '),
                        CASE WHEN role.{qi(pass_col)} IS NOT NULL THEN
                            concat(' {{ SET password_hash := ',
                                   quote_literal(role.{qi(pass_col)}),
                                   '}};')
                        ELSE ';' END
                    )
                END,
                '\n'
            ), '') str
            FROM ordered_roles
                JOIN {q(*roles)} role
                ON role.id = ordered_roles.id
        """

        super().__init__(
            name=('edgedb', '_describe_roles_as_ddl'),
            args=[],
            returns=('text'),
            # Stable because it's raising exceptions.
            volatility='stable',
            text=text)


class DumpSequencesFunction(dbops.Function):

    text = r"""
        SELECT
            string_agg(
                'SELECT std::sequence_reset('
                || 'INTROSPECT ' || edgedb.quote_name(seq.name)
                || (CASE WHEN seq_st.is_called
                    THEN ', ' || seq_st.last_value::text
                    ELSE '' END)
                || ');',
                E'\n'
            )
        FROM
            (SELECT
                id,
                name
             FROM
                edgedb."_SchemaScalarType"
             WHERE
                id = any("seqs")
            ) AS seq,
            LATERAL (
                SELECT
                    COALESCE(last_value, start_value)::text AS last_value,
                    last_value IS NOT NULL AS is_called
                FROM
                    pg_sequences,
                    LATERAL ROWS FROM (
                        edgedb.get_sequence_backend_name(seq.id)
                    ) AS seq_name(schema text, name text)
                WHERE
                    (pg_sequences.schemaname, pg_sequences.sequencename)
                    = (seq_name.schema, seq_name.name)
            ) AS seq_st
    """

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_dump_sequences'),
            args=[('seqs', ('uuid[]',))],
            returns=('text',),
            # Volatile because sequence state is volatile
            volatility='volatile',
            text=self.text,
        )


class SysConfigSourceType(dbops.Enum):
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_sys_config_source_t'),
            values=[
                'default',
                'postgres default',
                'postgres environment variable',
                'postgres configuration file',
                'environment variable',
                'command line',
                'postgres command line',
                'postgres global',
                'postgres client',
                'system override',
                'database',
                'postgres override',
                'postgres interactive',
                'postgres test',
                'session',
            ]
        )


class SysConfigScopeType(dbops.Enum):
    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_sys_config_scope_t'),
            values=[
                'INSTANCE',
                'DATABASE',
                'SESSION',
            ]
        )


class SysConfigValueType(dbops.CompositeType):
    """Type of values returned by _read_sys_config."""
    def __init__(self) -> None:
        super().__init__(name=('edgedb', '_sys_config_val_t'))

        self.add_columns([
            dbops.Column(name='name', type='text'),
            dbops.Column(name='value', type='jsonb'),
            dbops.Column(name='source', type='edgedb._sys_config_source_t'),
            dbops.Column(name='scope', type='edgedb._sys_config_scope_t'),
        ])


class SysConfigEntryType(dbops.CompositeType):
    """Type of values returned by _read_sys_config_full."""
    def __init__(self) -> None:
        super().__init__(name=('edgedb', '_sys_config_entry_t'))

        self.add_columns([
            dbops.Column(name='max_source', type='edgedb._sys_config_source_t'),
            dbops.Column(name='value', type='edgedb._sys_config_val_t'),
        ])


class IntervalToMillisecondsFunction(dbops.Function):
    """Cast an interval into milliseconds."""

    text = r'''
        SELECT
            trunc(extract(hours from "val"))::numeric * 3600000 +
            trunc(extract(minutes from "val"))::numeric * 60000 +
            trunc(extract(milliseconds from "val"))::numeric
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_interval_to_ms'),
            args=[('val', ('interval',))],
            returns=('numeric',),
            volatility='immutable',
            text=self.text,
        )


class SafeIntervalCastFunction(dbops.Function):
    """A safer text to interval casting implementaion.

    Casting large-unit durations (like '4032000000us') results in an error.
    Huge durations like this can be returned when introspecting current
    database config. Fix that by parsing the argument and using multiplication.
    """

    text = r'''
        SELECT
            CASE

                WHEN m.v[1] IS NOT NULL AND m.v[2] IS NOT NULL
                THEN
                    m.v[1]::numeric * ('1' || m.v[2])::interval

                ELSE
                    "val"::interval
            END
        FROM LATERAL (
            SELECT regexp_match(
                "val", '^(\d+)\s*(us|ms|s|min|h)$') AS v
        ) AS m
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_interval_safe_cast'),
            args=[('val', ('text',))],
            returns=('interval',),
            volatility='immutable',
            text=self.text,
        )


class ConvertPostgresConfigUnitsFunction(dbops.Function):
    """Convert duration/memory values to milliseconds/kilobytes.

    See https://www.postgresql.org/docs/12/config-setting.html
    for information about the units Postgres config system has.
    """

    text = r"""
    SELECT (
        CASE
            WHEN "unit" = any(ARRAY['us', 'ms', 's', 'min', 'h'])
            THEN to_jsonb(
                edgedb._interval_safe_cast(
                    ("value" * "multiplier")::text || "unit"
                )
            )

            WHEN "unit" = 'B'
            THEN to_jsonb(
                ("value" * "multiplier")::text || 'B'
            )

            WHEN "unit" = 'kB'
            THEN to_jsonb(
                ("value" * "multiplier")::text || 'KiB'
            )

            WHEN "unit" = 'MB'
            THEN to_jsonb(
                ("value" * "multiplier")::text || 'MiB'
            )

            WHEN "unit" = 'GB'
            THEN to_jsonb(
                ("value" * "multiplier")::text || 'GiB'
            )

            WHEN "unit" = 'TB'
            THEN to_jsonb(
                ("value" * "multiplier")::text || 'TiB'
            )

            WHEN "unit" = ''
            THEN trunc("value" * "multiplier")::text::jsonb

            ELSE edgedb.raise(
                NULL::jsonb,
                msg => (
                    'unknown configutation unit "' ||
                    COALESCE("unit", '<NULL>') ||
                    '"'
                )
            )
        END
    )
    """

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_convert_postgres_config_units'),
            args=[
                ('value', ('numeric',)),
                ('multiplier', ('numeric',)),
                ('unit', ('text',))
            ],
            returns=('jsonb',),
            volatility='immutable',
            text=self.text,
        )


class NormalizedPgSettingsView(dbops.View):
    """Just like `pg_settings` but with the parsed 'unit' column."""

    query = r'''
        SELECT
            s.name AS name,
            s.setting AS setting,
            s.vartype AS vartype,
            s.source AS source,
            unit.multiplier AS multiplier,
            unit.unit AS unit

        FROM pg_settings AS s,

        LATERAL (
            SELECT regexp_match(
                s.unit, '^(\d*)\s*([a-zA-Z]{1,3})$') AS v
        ) AS _unit,

        LATERAL (
            SELECT
                COALESCE(
                    CASE
                        WHEN _unit.v[1] = '' THEN 1
                        ELSE _unit.v[1]::int
                    END,
                    1
                ) AS multiplier,
                COALESCE(_unit.v[2], '') AS unit
        ) AS unit
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_normalized_pg_settings'),
            query=self.query,
        )


class InterpretConfigValueToJsonFunction(dbops.Function):
    """Convert a Postgres config value to jsonb.

    This function:

    * converts booleans to JSON true/false;
    * converts enums and strings to JSON strings;
    * converts real/integers to JSON numbers:
      - for durations: we always convert to milliseconds;
      - for memory size: we always convert to kilobytes;
      - already unitless numbers are left as is.

    See https://www.postgresql.org/docs/12/config-setting.html
    for information about the units Postgres config system has.
    """

    text = r"""
    SELECT (
        CASE
            WHEN "type" = 'bool'
            THEN (
                CASE
                WHEN lower("value") = any(ARRAY['on', 'true', 'yes', '1'])
                THEN 'true'
                ELSE 'false'
                END
            )::jsonb

            WHEN "type" = 'enum' OR "type" = 'string'
            THEN to_jsonb("value")

            WHEN "type" = 'integer' OR "type" = 'real'
            THEN edgedb._convert_postgres_config_units(
                    "value"::numeric, "multiplier"::numeric, "unit"
                 )

            ELSE
                edgedb.raise(
                    NULL::jsonb,
                    msg => (
                        'unknown configutation type "' ||
                        COALESCE("type", '<NULL>') ||
                        '"'
                    )
                )
        END
    )
    """

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_interpret_config_value_to_json'),
            args=[
                ('value', ('text',)),
                ('type', ('text',)),
                ('multiplier', ('int',)),
                ('unit', ('text',))
            ],
            returns=('jsonb',),
            volatility='immutable',
            text=self.text,
        )


class PostgresConfigValueToJsonFunction(dbops.Function):
    """Convert a Postgres setting to JSON value.

    Steps:

    * Lookup the `setting_name` in pg_settings to determine its
      type and unit.

    * Parse `setting_value` to see if it starts with numbers and ends
      with what looks like a unit.

    * Fetch the unit/multiplier pg_settings (well, from our view over it).

    * If `setting_value` has a unit, pass it to
      `_interpret_config_value_to_json`

    * If `setting_value` doesn't have a unit, pass it to
      `_interpret_config_value_to_json` along with the base unit/multiplier
      from pg_settings.

    * Then, the `_interpret_config_value_to_json` is capable of casting the
      value correctly based on the pg_settings type and the supplied
      unit/multiplier.
    """

    text = r"""
        SELECT
            (CASE

                WHEN parsed_value.unit != ''
                THEN
                    edgedb._interpret_config_value_to_json(
                        parsed_value.val,
                        settings.vartype,
                        1,
                        parsed_value.unit
                    )

                ELSE
                    edgedb._interpret_config_value_to_json(
                        "setting_value",
                        settings.vartype,
                        settings.multiplier,
                        settings.unit
                    )

            END)
        FROM
            (
                SELECT
                    epg_settings.vartype AS vartype,
                    epg_settings.multiplier AS multiplier,
                    epg_settings.unit AS unit
                FROM
                    edgedb._normalized_pg_settings AS epg_settings
                WHERE
                    epg_settings.name = "setting_name"
            ) AS settings,

            LATERAL (
                SELECT regexp_match(
                    "setting_value", '^(\d+)\s*([a-zA-Z]{0,3})$') AS v
            ) AS _unit,

            LATERAL (
                SELECT
                    COALESCE(_unit.v[1], "setting_value") AS val,
                    COALESCE(_unit.v[2], '') AS unit
            ) AS parsed_value
    """

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_postgres_config_value_to_json'),
            args=[
                ('setting_name', ('text',)),
                ('setting_value', ('text',)),
            ],
            returns=('jsonb',),
            volatility='volatile',
            text=self.text,
        )


class SysConfigFullFunction(dbops.Function):

    # This is a function because "_edgecon_state" is a temporary table
    # and therefore cannot be used in a view.

    text = f'''
    DECLARE
        query text;
    BEGIN

    query := $$
        WITH

        config_spec AS (
            SELECT
                s.key AS name,
                s.value->'default' AS default,
                (s.value->>'internal')::bool AS internal,
                (s.value->>'system')::bool AS system,
                (s.value->>'typeid')::uuid AS typeid,
                (s.value->>'typemod') AS typemod,
                (s.value->>'backend_setting') AS backend_setting
            FROM
                edgedbinstdata.instdata as id,
            LATERAL jsonb_each(id.json) AS s
            WHERE id.key LIKE 'configspec%'
        ),

        config_defaults AS (
            SELECT
                s.name AS name,
                s.default AS value,
                'default' AS source,
                s.backend_setting IS NOT NULL AS is_backend
            FROM
                config_spec s
        ),

        config_sys AS (
            SELECT
                s.key AS name,
                s.value AS value,
                'system override' AS source,
                config_spec.backend_setting IS NOT NULL AS is_backend
            FROM
                jsonb_each(
                    edgedb.get_database_metadata(
                        {ql(defines.EDGEDB_SYSTEM_DB)}
                    ) -> 'sysconfig'
                ) AS s
                INNER JOIN config_spec ON (config_spec.name = s.key)
        ),

        config_db AS (
            SELECT
                s.name AS name,
                s.value AS value,
                'database' AS source,
                config_spec.backend_setting IS NOT NULL AS is_backend
            FROM
                edgedb._db_config s
                INNER JOIN config_spec ON (config_spec.name = s.name)
        ),

        config_sess AS (
            SELECT
                s.name AS name,
                s.value AS value,
                (CASE
                    WHEN s.type = 'A' THEN 'command line'
                    WHEN s.type = 'E' THEN 'environment variable'
                    ELSE 'session'
                END) AS source,
                FALSE AS from_backend  -- only 'B' is for backend settings
            FROM
                _edgecon_state s
            WHERE
                s.type != 'B'
        ),

        pg_db_setting AS (
            SELECT
                spec.name,
                edgedb._postgres_config_value_to_json(
                    spec.backend_setting, nameval.value
                ) AS value,
                'database' AS source,
                TRUE AS is_backend
            FROM
                (SELECT
                    setconfig
                FROM
                    pg_db_role_setting
                WHERE
                    setdatabase = (
                        SELECT oid
                        FROM pg_database
                        WHERE datname = current_database()
                    )
                    AND setrole = 0
                ) AS cfg_array,
                LATERAL unnest(cfg_array.setconfig) AS cfg_set(s),
                LATERAL (
                    SELECT
                        split_part(cfg_set.s, '=', 1) AS name,
                        split_part(cfg_set.s, '=', 2) AS value
                ) AS nameval,
                LATERAL (
                    SELECT
                        config_spec.name,
                        config_spec.backend_setting
                    FROM
                        config_spec
                    WHERE
                        nameval.name = config_spec.backend_setting
                ) AS spec
        ),
    $$;

    IF fs_access THEN
        query := query || $$
            pg_conf_settings AS (
                SELECT
                    spec.name,
                    edgedb._postgres_config_value_to_json(
                        spec.backend_setting, setting
                    ) AS value,
                    'postgres configuration file' AS source,
                    TRUE AS is_backend
                FROM
                    pg_file_settings,
                    LATERAL (
                        SELECT
                            config_spec.name,
                            config_spec.backend_setting
                        FROM
                            config_spec
                        WHERE
                            pg_file_settings.name = config_spec.backend_setting
                    ) AS spec
                WHERE
                    sourcefile != ((
                        SELECT setting
                        FROM pg_settings WHERE name = 'data_directory'
                    ) || '/postgresql.auto.conf')
                    AND applied
            ),

            pg_auto_conf_settings AS (
                SELECT
                    spec.name,
                    edgedb._postgres_config_value_to_json(
                        spec.backend_setting, setting
                    ) AS value,
                    'system override' AS source,
                    TRUE AS is_backend
                FROM
                    pg_file_settings,
                    LATERAL (
                        SELECT
                            config_spec.name,
                            config_spec.backend_setting
                        FROM
                            config_spec
                        WHERE
                            pg_file_settings.name = config_spec.backend_setting
                    ) AS spec
                WHERE
                    sourcefile = ((
                        SELECT setting
                        FROM pg_settings WHERE name = 'data_directory'
                    ) || '/postgresql.auto.conf')
                    AND applied
            ),
        $$;
    END IF;

    query := query || $$
        pg_config AS (
            SELECT
                spec.name,
                edgedb._interpret_config_value_to_json(
                    settings.setting,
                    settings.vartype,
                    settings.multiplier,
                    settings.unit
                ) AS value,
                source AS source,
                TRUE AS is_backend
            FROM
                (
                    SELECT
                        epg_settings.name AS name,
                        epg_settings.unit AS unit,
                        epg_settings.multiplier AS multiplier,
                        epg_settings.vartype AS vartype,
                        epg_settings.setting AS setting,
                        (CASE
                            WHEN epg_settings.source = 'session' THEN
                                epg_settings.source
                            ELSE
                                'postgres ' || epg_settings.source
                        END) AS source
                    FROM
                        edgedb._normalized_pg_settings AS epg_settings
                    WHERE
                        epg_settings.source != 'database'
                ) AS settings,

                LATERAL (
                    SELECT
                        config_spec.name
                    FROM
                        config_spec
                    WHERE
                        settings.name = config_spec.backend_setting
                ) AS spec
            ),

        edge_all_settings AS MATERIALIZED (
            SELECT
                q.*
            FROM
                (
                    SELECT * FROM config_defaults UNION ALL
                    SELECT * FROM config_sys UNION ALL
                    SELECT * FROM config_db UNION ALL
                    SELECT * FROM config_sess
                ) AS q
            WHERE
                NOT q.is_backend
        ),

    $$;

    IF fs_access THEN
        query := query || $$
            pg_all_settings AS MATERIALIZED (
                SELECT
                    q.*
                FROM
                    (
                        SELECT * FROM pg_db_setting UNION ALL
                        SELECT * FROM pg_conf_settings UNION ALL
                        SELECT * FROM pg_auto_conf_settings UNION ALL
                        SELECT * FROM pg_config
                    ) AS q
                WHERE
                    q.is_backend
            )
        $$;
    ELSE
        query := query || $$
            pg_all_settings AS MATERIALIZED (
                SELECT
                    q.*
                FROM
                    (
                        -- config_sys is here, because there
                        -- is no other way to read instance-level
                        -- configuration overrides.
                        SELECT * FROM config_sys UNION ALL
                        SELECT * FROM pg_db_setting UNION ALL
                        SELECT * FROM pg_config
                    ) AS q
                WHERE
                    q.is_backend
            )
        $$;
    END IF;

    query := query || $$
        SELECT
            max_source AS max_source,
            (q.name,
            q.value,
            q.source,
            (CASE
                WHEN q.source < 'database'::edgedb._sys_config_source_t THEN
                    'INSTANCE'
                WHEN q.source = 'database'::edgedb._sys_config_source_t THEN
                    'DATABASE'
                ELSE
                    'SESSION'
            END)::edgedb._sys_config_scope_t
            )::edgedb._sys_config_val_t as value
        FROM
            unnest($2) as max_source,
            LATERAL (SELECT
                u.name,
                u.value,
                u.source::edgedb._sys_config_source_t,
                row_number() OVER (
                    PARTITION BY u.name
                    ORDER BY u.source::edgedb._sys_config_source_t DESC
                ) AS n
            FROM
                (SELECT
                    *
                FROM
                    (
                        SELECT * FROM edge_all_settings UNION ALL
                        SELECT * FROM pg_all_settings
                    ) AS q
                WHERE
                    q.value IS NOT NULL
                    AND ($1 IS NULL OR
                        q.source::edgedb._sys_config_source_t = any($1)
                    )
                    AND (max_source IS NULL OR
                        q.source::edgedb._sys_config_source_t <= max_source
                    )
                ) AS u
            ) AS q
        WHERE
            q.n = 1;
    $$;

    RETURN QUERY EXECUTE query USING source_filter, max_sources;
    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_read_sys_config_full'),
            args=[
                (
                    'source_filter',
                    ('edgedb', '_sys_config_source_t[]',),
                    'NULL',
                ),
                (
                    'max_sources',
                    ('edgedb', '_sys_config_source_t[]'),
                    'NULL',
                ),
                (
                    'fs_access',
                    ('bool',),
                    'TRUE',
                )
            ],
            returns=('edgedb', '_sys_config_entry_t'),
            set_returning=True,
            language='plpgsql',
            volatility='volatile',
            text=self.text,
        )


class SysConfigUncachedFunction(dbops.Function):

    text = f'''
    DECLARE
        backend_caps bigint;
    BEGIN

    backend_caps := edgedb.get_backend_capabilities();
    IF (backend_caps
        & {int(params.BackendCapabilities.CONFIGFILE_ACCESS)}) != 0
    THEN
        RETURN QUERY
        SELECT *
        FROM edgedb._read_sys_config_full(source_filter, max_sources, TRUE);
    ELSE
        RETURN QUERY
        SELECT *
        FROM edgedb._read_sys_config_full(source_filter, max_sources, FALSE);
    END IF;

    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_read_sys_config_uncached'),
            args=[
                (
                    'source_filter',
                    ('edgedb', '_sys_config_source_t[]',),
                    'NULL',
                ),
                (
                    'max_sources',
                    ('edgedb', '_sys_config_source_t[]'),
                    'NULL',
                ),
            ],
            returns=('edgedb', '_sys_config_entry_t'),
            set_returning=True,
            language='plpgsql',
            volatility='volatile',
            text=self.text,
        )


class SysConfigFunction(dbops.Function):

    text = f'''
    DECLARE
    BEGIN

    -- Only bother caching the source_filter IS NULL case, since that
    -- is what drives the config views. source_filter is used in
    -- DESCRIBE CONFIG
    IF source_filter IS NOT NULL OR array_position(
     ARRAY[NULL, 'database', 'system override']::edgedb._sys_config_source_t[],
      max_source) IS NULL
     THEN
        RETURN QUERY
        SELECT
          (c.value).name, (c.value).value, (c.value).source, (c.value).scope
        FROM edgedb._read_sys_config_uncached(
          source_filter, ARRAY[max_source]) AS c;
        RETURN;
    END IF;

    IF count(*) = 0 FROM "_config_cache" c
       WHERE source IS NOT DISTINCT FROM max_source
    THEN
        INSERT INTO "_config_cache"
        SELECT (s.max_source), (s.value)
        FROM edgedb._read_sys_config_uncached(
          source_filter, ARRAY[
            NULL, 'database', 'system override']::edgedb._sys_config_source_t[])
             AS s;
    END IF;

    RETURN QUERY
    SELECT (c.value).name, (c.value).value, (c.value).source, (c.value).scope
    FROM "_config_cache" c WHERE source IS NOT DISTINCT FROM max_source;

    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_read_sys_config'),
            args=[
                (
                    'source_filter',
                    ('edgedb', '_sys_config_source_t[]',),
                    'NULL',
                ),
                (
                    'max_source',
                    ('edgedb', '_sys_config_source_t'),
                    'NULL',
                ),
            ],
            returns=('edgedb', '_sys_config_val_t'),
            set_returning=True,
            language='plpgsql',
            volatility='volatile',
            text=self.text,
        )


class SysClearConfigCacheFunction(dbops.Function):

    text = f'''
    DECLARE
    BEGIN

    DELETE FROM "_config_cache" c;
    RETURN true;

    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_clear_sys_config_cache'),
            args=[],
            returns=("boolean"),
            set_returning=False,
            language='plpgsql',
            volatility='volatile',
            text=self.text,
        )


class ResetSessionConfigFunction(dbops.Function):

    text = f'''
        RESET ALL
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_reset_session_config'),
            args=[],
            returns=('void',),
            language='sql',
            volatility='volatile',
            text=self.text,
        )


# TODO: Support extension-defined configs that affect the backend
# Not needed for supporting auth, so can skip temporarily.
# If perf seems to matter, can hardcode things for base config
# and consult json for just extension stuff.
class ApplySessionConfigFunction(dbops.Function):
    """Apply an EdgeDB config setting to the backend, if possible.

    The function accepts any EdgeDB config name/value pair. If this
    specific config setting happens to be implemented via a backend
    setting, it would be applied to the current PostgreSQL session.
    If the config setting doesn't reflect into a backend setting the
    function is a no-op.

    The function always returns the passed config name, unmodified
    (this simplifies using the function in queries.)
    """

    def __init__(self, config_spec: edbconfig.Spec) -> None:

        backend_settings = {}
        for setting_name in config_spec:
            setting = config_spec[setting_name]

            if setting.backend_setting and not setting.system:
                backend_settings[setting_name] = setting.backend_setting

        variants_list = []
        for setting_name in backend_settings:
            setting = config_spec[setting_name]

            valql = '"value"->>0'
            if (
                isinstance(setting.type, type)
                and issubclass(setting.type, statypes.Duration)
            ):
                valql = f"""
                    edgedb._interval_to_ms(({valql})::interval)::text || 'ms'
                """

            variants_list.append(f'''
                WHEN "name" = {ql(setting_name)}
                THEN
                    pg_catalog.set_config(
                        {ql(setting.backend_setting)}::text,
                        {valql},
                        false
                    )
            ''')

        variants = "\n".join(variants_list)
        text = f'''
        SELECT (
            CASE
                WHEN "name" = any(
                    ARRAY[{",".join(ql(str(bs)) for bs in backend_settings)}]
                )
                THEN (
                    CASE
                        WHEN
                            (CASE
                                {variants}
                            END) IS NULL
                        THEN "name"
                        ELSE "name"
                    END
                )

                ELSE "name"
            END
        )
        '''

        super().__init__(
            name=('edgedb', '_apply_session_config'),
            args=[
                ('name', ('text',)),
                ('value', ('jsonb',)),
            ],
            returns=('text',),
            language='sql',
            volatility='volatile',
            text=text,
        )


class SysGetTransactionIsolation(dbops.Function):
    "Get transaction isolation value as text compatible with EdgeDB's enum."
    text = r'''
        SELECT
            CASE setting
                WHEN 'repeatable read' THEN 'RepeatableRead'
                WHEN 'serializable' THEN 'Serializable'
                ELSE (
                    SELECT edgedb.raise(
                        NULL::text,
                        msg => (
                            'unknown transaction isolation level "'
                            || setting || '"'
                        )
                    )
                )
            END
        FROM pg_settings
        WHERE name = 'transaction_isolation'
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_get_transaction_isolation'),
            args=[],
            returns=('text',),
            # This function only reads from a table.
            volatility='stable',
            text=self.text)


class GetCachedReflection(dbops.Function):
    "Return a list of existing schema reflection helpers."
    text = '''
        SELECT
            substring(proname, '__rh_#"%#"', '#') AS eql_hash,
            proargnames AS argnames
        FROM
            pg_proc
            INNER JOIN pg_namespace ON (pronamespace = pg_namespace.oid)
        WHERE
            proname LIKE '\\_\\_rh\\_%'
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_get_cached_reflection'),
            args=[],
            returns=('record',),
            set_returning=True,
            # This function only reads from a table.
            volatility='stable',
            text=self.text,
        )


class GetBaseScalarTypeMap(dbops.Function):
    """Return a map of base EdgeDB scalar type ids to Postgres type names."""

    text = f'''
        VALUES
            {", ".join(
                f"""(
                    {ql(str(k))}::uuid,
                    {
                        ql(f'{v[0]}.{v[1]}') if len(v) == 2
                        else ql(f'pg_catalog.{v[0]}')
                    }
                )"""
            for k, v in types.base_type_name_map.items())}
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_get_base_scalar_type_map'),
            args=[],
            returns=('record',),
            set_returning=True,
            volatility='immutable',
            text=self.text,
        )


class GetTypeToRangeNameMap(dbops.Function):
    """Return a map of type names to the name of the associated range type"""

    text = f'''
        VALUES
            {", ".join(
                f"""(
                    {
                        ql(f'{k[0]}.{k[1]}') if len(k) == 2
                        else ql(f'pg_catalog.{k[0]}')
                    },
                    {
                        ql(f'{v[0]}.{v[1]}') if len(v) == 2
                        else ql(f'pg_catalog.{v[0]}')
                    }
                )"""
            for k, v in types.type_to_range_name_map.items())}
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_get_type_to_range_type_map'),
            args=[],
            returns=('record',),
            set_returning=True,
            volatility='immutable',
            text=self.text,
        )


class GetTypeToMultiRangeNameMap(dbops.Function):
    "Return a map of type names to the name of the associated multirange type"

    text = f'''
        VALUES
            {", ".join(
                f"""(
                    {
                        ql(f'{k[0]}.{k[1]}') if len(k) == 2
                        else ql(f'pg_catalog.{k[0]}')
                    },
                    {
                        ql(f'{v[0]}.{v[1]}') if len(v) == 2
                        else ql(f'pg_catalog.{v[0]}')
                    }
                )"""
            for k, v in types.type_to_multirange_name_map.items())}
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_get_type_to_multirange_type_map'),
            args=[],
            returns=('record',),
            set_returning=True,
            volatility='immutable',
            text=self.text,
        )


class GetPgTypeForEdgeDBTypeFunction(dbops.Function):
    """Return Postgres OID representing a given EdgeDB type."""

    text = f'''
        SELECT
            coalesce(
                sql_type::regtype::oid,
                (
                    SELECT
                        tn::regtype::oid
                    FROM
                        edgedb._get_base_scalar_type_map()
                            AS m(tid uuid, tn text)
                    WHERE
                        m.tid = "typeid"
                ),
                (
                    SELECT
                        typ.oid
                    FROM
                        pg_catalog.pg_type typ
                    WHERE
                        typ.typname = "typeid"::text || '_domain'
                        OR typ.typname = "typeid"::text || '_t'
                ),
                (
                    SELECT
                        typ.typarray
                    FROM
                        pg_catalog.pg_type typ
                    WHERE
                        "kind" = 'schema::Array'
                         AND (
                            typ.typname = "elemid"::text || '_domain'
                            OR typ.typname = "elemid"::text || '_t'
                            OR typ.oid = (
                                SELECT
                                    tn::regtype::oid
                                FROM
                                    edgedb._get_base_scalar_type_map()
                                        AS m(tid uuid, tn text)
                                WHERE
                                    tid = "elemid"
                            )
                        )
                ),
                (
                    SELECT
                        rng.rngtypid
                    FROM
                        pg_catalog.pg_range rng
                    WHERE
                        "kind" = 'schema::Range'
                        -- For ranges, we need to do the lookup based on
                        -- our internal map of elem names to range names,
                        -- because we use the builtin daterange as the range
                        -- for edgedb.date_t.
                        AND rng.rngtypid = (
                            SELECT
                                rn::regtype::oid
                            FROM
                                edgedb._get_base_scalar_type_map()
                                    AS m(tid uuid, tn text)
                            INNER JOIN
                                edgedb._get_type_to_range_type_map()
                                    AS m2(tn2 text, rn text)
                                ON tn = tn2
                            WHERE
                                tid = "elemid"
                        )
                ),
                (
                    SELECT
                        rng.rngmultitypid
                    FROM
                        pg_catalog.pg_range rng
                    WHERE
                        "kind" = 'schema::MultiRange'
                        -- For multiranges, we need to do the lookup based on
                        -- our internal map of elem names to range names,
                        -- because we use the builtin daterange as the range
                        -- for edgedb.date_t.
                        AND rng.rngmultitypid = (
                            SELECT
                                rn::regtype::oid
                            FROM
                                edgedb._get_base_scalar_type_map()
                                    AS m(tid uuid, tn text)
                            INNER JOIN
                                edgedb._get_type_to_multirange_type_map()
                                    AS m2(tn2 text, rn text)
                                ON tn = tn2
                            WHERE
                                tid = "elemid"
                        )
                ),
                edgedb.raise(
                    NULL::bigint,
                    'invalid_parameter_value',
                    msg => (
                        format(
                            'cannot determine OID of EdgeDB type %L',
                            "typeid"::text
                        )
                    )
                )
            )::bigint
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'get_pg_type_for_edgedb_type'),
            args=[
                ('typeid', ('uuid',)),
                ('kind', ('text',)),
                ('elemid', ('uuid',)),
                ('sql_type', ('text',)),
            ],
            returns=('bigint',),
            volatility='stable',
            text=self.text,
        )


class FTSParseQueryFunction(dbops.Function):
    """Return tsquery representing the given FTS input query."""

    text = r'''
    DECLARE
        parts text[];
        exclude text;
        term text;
        rest text;
        cur_op text := NULL;
        default_op text;
        tsq tsquery;
        el tsquery;
        result tsquery := ''::tsquery;

    BEGIN
        IF q IS NULL OR q = '' THEN
            RETURN result;
        END IF;

        -- Break up the query string into the current term, optional next
        -- operator and the rest.
        parts := regexp_match(
            q, $$^(-)?((?:"[^"]*")|(?:\S+))\s*(OR|AND)?\s*(.*)$$
        );
        exclude := parts[1];
        term := parts[2];
        cur_op := parts[3];
        rest := parts[4];

        IF starts_with(term, '"') THEN
            -- match as a phrase
            tsq := phraseto_tsquery(language, trim(both '"' from term));
        ELSE
            tsq := to_tsquery(language, term);
        END IF;

        IF exclude IS NOT NULL THEN
            tsq := !!tsq;
        END IF;

        -- figure out the operator between the current term and the next one
        IF rest = '' THEN
            -- base case, one one term left, so we ignore the cur_op even if
            -- present
            IF prev_op = 'OR' THEN
                -- explicit 'OR' terms are "should"
                should := array_append(should, tsq);
            ELSIF starts_with(term, '"')
               OR exclude IS NOT NULL
               OR prev_op = 'AND' THEN
                -- phrases, exclusions and 'AND' terms are "must"
                must := array_append(must, tsq);
            ELSE
                -- regular terms are "should" by default
                should := array_append(should, tsq);
            END IF;
        ELSE
            -- recursion

            IF prev_op = 'OR' OR cur_op = 'OR' THEN
                -- if at least one of the suprrounding operators is 'OR',
                -- then the phrase is put into "should" category
                should := array_append(should, tsq);
            ELSIF prev_op = 'AND' OR cur_op = 'AND' THEN
                -- if at least one of the suprrounding operators is 'AND',
                -- then the phrase is put into "must" category
                must := array_append(must, tsq);
            ELSIF starts_with(term, '"') OR exclude IS NOT NULL THEN
                -- phrases and exclusions are "must"
                must := array_append(must, tsq);
            ELSE
                -- regular terms are "should" by default
                should := array_append(should, tsq);
            END IF;

            RETURN edgedb.fts_parse_query(
                rest, language, must, should, cur_op);
        END IF;

        FOREACH el IN ARRAY should
        LOOP
            result := result || el;
        END LOOP;

        FOREACH el IN ARRAY must
        LOOP
            result := result && el;
        END LOOP;

        RETURN result;

    END;
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'fts_parse_query'),
            args=[
                ('q', ('text',)),
                ('language', ('regconfig',), "'english'"),
                ('must', ('tsquery[]',), 'array[]::tsquery[]'),
                ('should', ('tsquery[]',), 'array[]::tsquery[]'),
                ('prev_op', ('text',), 'NULL'),
            ],
            returns=('tsquery',),
            volatility='immutable',
            language='plpgsql',
            text=self.text,
        )


class FTSNormalizeWeightFunction(dbops.Function):
    """Normalize an array of weights to be a 4-value weight array."""

    text = r'''
    SELECT
        CASE COALESCE(array_length(weights, 1), 0)
            WHEN 0 THEN array[1, 1, 1, 1]::float4[]
            WHEN 1 THEN array[0, 0, 0, weights[1]]::float4[]
            WHEN 2 THEN array[0, 0, weights[2], weights[1]]::float4[]
            WHEN 3 THEN array[0, weights[3], weights[2], weights[1]]::float4[]
            ELSE (
                WITH raw as (
                    SELECT w
                    FROM UNNEST(weights) AS w
                    ORDER BY w DESC
                )
                SELECT array_prepend(rest.w, first.arrw)::float4[]
                FROM
                (
                    SELECT array_agg(rw1.w) as arrw
                    FROM (
                        SELECT w
                        FROM (SELECT w FROM raw LIMIT 3) as rw0
                        ORDER BY w ASC
                    ) as rw1
                ) AS first,
                (
                    SELECT avg(rw2.w) as w
                    FROM (SELECT w FROM raw OFFSET 3) as rw2
                ) AS rest
            )
        END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'fts_normalize_weights'),
            args=[
                ('weights', ('float8[]',)),
            ],
            returns=('float4[]',),
            volatility='immutable',
            text=self.text,
        )


class FTSNormalizeDocFunction(dbops.Function):
    """Normalize a document based on an array of weights."""

    text = r'''
    SELECT
        CASE COALESCE(array_length(doc, 1), 0)
            WHEN 0 THEN ''::tsvector
            WHEN 1 THEN setweight(to_tsvector(language, doc[1]), 'A')
            WHEN 2 THEN (
                setweight(to_tsvector(language, doc[1]), 'A') ||
                setweight(to_tsvector(language, doc[2]), 'B')
            )
            WHEN 3 THEN (
                setweight(to_tsvector(language, doc[1]), 'A') ||
                setweight(to_tsvector(language, doc[2]), 'B') ||
                setweight(to_tsvector(language, doc[3]), 'C')
            )
            ELSE (
                WITH raw as (
                    SELECT d.v as t
                    FROM UNNEST(doc) WITH ORDINALITY AS d(v, n)
                    LEFT JOIN UNNEST(weights) WITH ORDINALITY AS w(v, n)
                    ON d.n = w.n
                    ORDER BY w.v DESC
                )
                SELECT
                    setweight(to_tsvector(language, d.arr[1]), 'A') ||
                    setweight(to_tsvector(language, d.arr[2]), 'B') ||
                    setweight(to_tsvector(language, d.arr[3]), 'C') ||
                    setweight(to_tsvector(language,
                                          array_to_string(d.arr[4:], ' ')),
                              'D')
                FROM
                (
                    SELECT array_agg(raw.t) as arr
                    FROM raw
                ) AS d
            )
        END
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'fts_normalize_doc'),
            args=[
                ('doc', ('text[]',)),
                ('weights', ('float8[]',)),
                ('language', ('regconfig',)),
            ],
            returns=('tsvector',),
            volatility='stable',
            text=self.text,
        )


class FTSToRegconfig(dbops.Function):
    """
    Converts ISO 639-3 language identifiers into a regconfig.
    Defaults to english.
    Identifiers prefixed with 'xxx_' have the prefix stripped and the remainder
    used as regconfg identifier.
    """

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'fts_to_regconfig'),
            args=[
                ('language', ('text',)),
            ],
            returns=('regconfig',),
            volatility='immutable',
            text='''
            SELECT CASE
                WHEN language ILIKE 'xxx_%' THEN SUBSTR(language, 4)
                ELSE (CASE LOWER(language)
                    WHEN 'ara' THEN 'arabic'
                    WHEN 'hye' THEN 'armenian'
                    WHEN 'eus' THEN 'basque'
                    WHEN 'cat' THEN 'catalan'
                    WHEN 'dan' THEN 'danish'
                    WHEN 'nld' THEN 'dutch'
                    WHEN 'eng' THEN 'english'
                    WHEN 'fin' THEN 'finnish'
                    WHEN 'fra' THEN 'french'
                    WHEN 'deu' THEN 'german'
                    WHEN 'ell' THEN 'greek'
                    WHEN 'hin' THEN 'hindi'
                    WHEN 'hun' THEN 'hungarian'
                    WHEN 'ind' THEN 'indonesian'
                    WHEN 'gle' THEN 'irish'
                    WHEN 'ita' THEN 'italian'
                    WHEN 'lit' THEN 'lithuanian'
                    WHEN 'npi' THEN 'nepali'
                    WHEN 'nor' THEN 'norwegian'
                    WHEN 'por' THEN 'portuguese'
                    WHEN 'ron' THEN 'romanian'
                    WHEN 'rus' THEN 'russian'
                    WHEN 'srp' THEN 'serbian'
                    WHEN 'spa' THEN 'spanish'
                    WHEN 'swe' THEN 'swedish'
                    WHEN 'tam' THEN 'tamil'
                    WHEN 'tur' THEN 'turkish'
                    WHEN 'yid' THEN 'yiddish'
                    ELSE 'english' END
                )
            END::pg_catalog.regconfig;
            ''',
        )


class FormatTypeFunction(dbops.Function):
    """Used instead of pg_catalog.format_type in pg_dump."""

    text = r'''
    SELECT
        CASE WHEN t.typcategory = 'A'
        THEN (
            SELECT
                quote_ident(nspname) || '.' ||
                quote_ident(el.typname) || tm.mod || '[]'
            FROM edgedbsql.pg_namespace
            WHERE oid = el.typnamespace
        )
        ELSE (
            SELECT
                quote_ident(nspname) || '.' ||
                quote_ident(t.typname) || tm.mod
            FROM edgedbsql.pg_namespace
            WHERE oid = t.typnamespace
        )
        END
    FROM
        (
            SELECT
                CASE WHEN typemod >= 0
                THEN '(' || typemod::text || ')'
                ELSE ''
                END AS mod
        ) as tm,
        edgedbsql.pg_type t
    LEFT JOIN edgedbsql.pg_type el ON t.typelem = el.oid
    WHERE t.oid = typeoid
    '''

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', '_format_type'),
            args=[
                ('typeoid', ('oid',)),
                ('typemod', ('integer',)),
            ],
            returns=('text',),
            volatility='stable',
            text=self.text,
        )


class UuidGenerateV1mcFunction(dbops.Function):
    def __init__(self, ext_schema: str) -> None:
        super().__init__(
            name=('edgedb', 'uuid_generate_v1mc'),
            args=[],
            returns=('uuid',),
            volatility='volatile',
            language='sql',
            strict=True,
            parallel_safe=True,
            text=f'SELECT "{ext_schema}".uuid_generate_v1mc();'
        )


class UuidGenerateV4Function(dbops.Function):
    def __init__(self, ext_schema: str) -> None:
        super().__init__(
            name=('edgedb', 'uuid_generate_v4'),
            args=[],
            returns=('uuid',),
            volatility='volatile',
            language='sql',
            strict=True,
            parallel_safe=True,
            text=f'SELECT "{ext_schema}".uuid_generate_v4();'
        )


class UuidGenerateV5Function(dbops.Function):
    def __init__(self, ext_schema: str) -> None:
        super().__init__(
            name=('edgedb', 'uuid_generate_v5'),
            args=[
                ('namespace', ('uuid',)),
                ('name', ('text',)),
            ],
            returns=('uuid',),
            volatility='immutable',
            language='sql',
            strict=True,
            parallel_safe=True,
            text=f'SELECT "{ext_schema}".uuid_generate_v5(namespace, name);'
        )


class PadBase64StringFunction(dbops.Function):
    text = r"""
        WITH
            l AS (SELECT pg_catalog.length("s") % 4 AS r),
            p AS (
                SELECT
                    (CASE WHEN l.r > 0 THEN repeat('=', (4 - l.r))
                    ELSE '' END) AS p
                FROM
                    l
            )
        SELECT
            "s" || p.p
        FROM
            p
    """

    def __init__(self) -> None:
        super().__init__(
            name=('edgedb', 'pad_base64_string'),
            args=[
                ('s', ('text',)),
            ],
            returns=('text',),
            volatility='immutable',
            language='sql',
            strict=True,
            parallel_safe=True,
            text=self.text,
        )


async def bootstrap(
    conn: PGConnection,
    config_spec: edbconfig.Spec,
) -> None:
    cmds = [
        dbops.CreateSchema(name='edgedb'),
        dbops.CreateSchema(name='edgedbpub'),
        dbops.CreateSchema(name='edgedbstd'),
        dbops.CreateSchema(name='edgedbsql'),
        dbops.CreateView(NormalizedPgSettingsView()),
        dbops.CreateTable(DBConfigTable()),
        dbops.CreateTable(DMLDummyTable()),
        dbops.Query(DMLDummyTable.SETUP_QUERY),
        dbops.CreateFunction(UuidGenerateV1mcFunction('edgedbext')),
        dbops.CreateFunction(UuidGenerateV4Function('edgedbext')),
        dbops.CreateFunction(UuidGenerateV5Function('edgedbext')),
        dbops.CreateFunction(IntervalToMillisecondsFunction()),
        dbops.CreateFunction(SafeIntervalCastFunction()),
        dbops.CreateFunction(QuoteIdentFunction()),
        dbops.CreateFunction(QuoteNameFunction()),
        dbops.CreateFunction(AlterCurrentDatabaseSetString()),
        dbops.CreateFunction(AlterCurrentDatabaseSetStringArray()),
        dbops.CreateFunction(AlterCurrentDatabaseSetNonArray()),
        dbops.CreateFunction(AlterCurrentDatabaseSetArray()),
        dbops.CreateFunction(GetBackendCapabilitiesFunction()),
        dbops.CreateFunction(GetBackendTenantIDFunction()),
        dbops.CreateFunction(GetDatabaseBackendNameFunction()),
        dbops.CreateFunction(GetRoleBackendNameFunction()),
        dbops.CreateFunction(GetUserSequenceBackendNameFunction()),
        dbops.CreateFunction(GetStdModulesFunction()),
        dbops.CreateFunction(GetObjectMetadata()),
        dbops.CreateFunction(GetColumnMetadata()),
        dbops.CreateFunction(GetSharedObjectMetadata()),
        dbops.CreateFunction(GetDatabaseMetadataFunction()),
        dbops.CreateFunction(GetCurrentDatabaseFunction()),
        dbops.CreateFunction(RaiseExceptionFunction()),
        dbops.CreateFunction(RaiseExceptionOnNullFunction()),
        dbops.CreateFunction(RaiseExceptionOnNotNullFunction()),
        dbops.CreateFunction(RaiseExceptionOnEmptyStringFunction()),
        dbops.CreateFunction(AssertJSONTypeFunction()),
        dbops.CreateFunction(ExtractJSONScalarFunction()),
        dbops.CreateFunction(NormalizeNameFunction()),
        dbops.CreateFunction(GetNameModuleFunction()),
        dbops.CreateFunction(NullIfArrayNullsFunction()),
        dbops.CreateDomain(BigintDomain()),
        dbops.CreateDomain(ConfigMemoryDomain()),
        dbops.CreateDomain(TimestampTzDomain()),
        dbops.CreateDomain(TimestampDomain()),
        dbops.CreateDomain(DateDomain()),
        dbops.CreateDomain(DurationDomain()),
        dbops.CreateDomain(RelativeDurationDomain()),
        dbops.CreateDomain(DateDurationDomain()),
        dbops.CreateFunction(StrToConfigMemoryFunction()),
        dbops.CreateFunction(ConfigMemoryToStrFunction()),
        dbops.CreateFunction(StrToBigint()),
        dbops.CreateFunction(StrToDecimal()),
        dbops.CreateFunction(StrToInt64NoInline()),
        dbops.CreateFunction(StrToInt32NoInline()),
        dbops.CreateFunction(StrToInt16NoInline()),
        dbops.CreateFunction(StrToFloat64NoInline()),
        dbops.CreateFunction(StrToFloat32NoInline()),
        dbops.CreateFunction(NormalizeArrayIndexFunction()),
        dbops.CreateFunction(NormalizeArraySliceIndexFunction()),
        dbops.CreateFunction(IntOrNullFunction()),
        dbops.CreateFunction(ArrayIndexWithBoundsFunction()),
        dbops.CreateFunction(ArraySliceFunction()),
        dbops.CreateFunction(StringIndexWithBoundsFunction()),
        dbops.CreateFunction(LengthStringProxyFunction()),
        dbops.CreateFunction(LengthBytesProxyFunction()),
        dbops.CreateFunction(SubstrProxyFunction()),
        dbops.CreateFunction(StringSliceImplFunction()),
        dbops.CreateFunction(StringSliceFunction()),
        dbops.CreateFunction(BytesSliceFunction()),
        dbops.CreateFunction(JSONIndexByTextFunction()),
        dbops.CreateFunction(JSONIndexByIntFunction()),
        dbops.CreateFunction(JSONSliceFunction()),
        dbops.CreateFunction(DatetimeInFunction()),
        dbops.CreateFunction(DurationInFunction()),
        dbops.CreateFunction(DateDurationInFunction()),
        dbops.CreateFunction(LocalDatetimeInFunction()),
        dbops.CreateFunction(LocalDateInFunction()),
        dbops.CreateFunction(LocalTimeInFunction()),
        dbops.CreateFunction(ToTimestampTZCheck()),
        dbops.CreateFunction(ToDatetimeFunction()),
        dbops.CreateFunction(ToLocalDatetimeFunction()),
        dbops.CreateFunction(StrToBool()),
        dbops.CreateFunction(BytesIndexWithBoundsFunction()),
        dbops.CreateEnum(SysConfigSourceType()),
        dbops.CreateEnum(SysConfigScopeType()),
        dbops.CreateCompositeType(SysConfigValueType()),
        dbops.CreateCompositeType(SysConfigEntryType()),
        dbops.CreateFunction(ConvertPostgresConfigUnitsFunction()),
        dbops.CreateFunction(InterpretConfigValueToJsonFunction()),
        dbops.CreateFunction(PostgresConfigValueToJsonFunction()),
        dbops.CreateFunction(SysConfigFullFunction()),
        dbops.CreateFunction(SysConfigUncachedFunction()),
        dbops.Query(pgcon.SETUP_CONFIG_CACHE_SCRIPT),
        dbops.CreateFunction(SysConfigFunction()),
        dbops.CreateFunction(SysClearConfigCacheFunction()),
        dbops.CreateFunction(ResetSessionConfigFunction()),
        dbops.CreateFunction(ApplySessionConfigFunction(config_spec)),
        dbops.CreateFunction(SysGetTransactionIsolation()),
        dbops.CreateFunction(GetCachedReflection()),
        dbops.CreateFunction(GetBaseScalarTypeMap()),
        dbops.CreateFunction(GetTypeToRangeNameMap()),
        dbops.CreateFunction(GetTypeToMultiRangeNameMap()),
        dbops.CreateFunction(GetPgTypeForEdgeDBTypeFunction()),
        dbops.CreateFunction(DescribeRolesAsDDLFunctionForwardDecl()),
        dbops.CreateRange(Float32Range()),
        dbops.CreateRange(Float64Range()),
        dbops.CreateRange(DatetimeRange()),
        dbops.CreateRange(LocalDatetimeRange()),
        dbops.CreateFunction(RangeToJsonFunction()),
        dbops.CreateFunction(MultiRangeToJsonFunction()),
        dbops.CreateFunction(RangeValidateFunction()),
        dbops.CreateFunction(RangeUnpackLowerValidateFunction()),
        dbops.CreateFunction(RangeUnpackUpperValidateFunction()),
        dbops.CreateFunction(FTSParseQueryFunction()),
        dbops.CreateFunction(FTSNormalizeWeightFunction()),
        dbops.CreateFunction(FTSNormalizeDocFunction()),
        dbops.CreateFunction(FTSToRegconfig()),
        dbops.CreateFunction(PadBase64StringFunction()),
    ]
    commands = dbops.CommandGroup()
    commands.add_commands(cmds)

    block = dbops.PLTopBlock()
    commands.generate(block)
    await _execute_block(conn, block)


async def create_pg_extensions(
    conn: PGConnection,
    backend_params: params.BackendRuntimeParams,
) -> None:
    inst_params = backend_params.instance_params
    ext_schema = inst_params.ext_schema
    # Both the extension schema, and the desired extension
    # might already exist in a single database backend,
    # attempt to create things conditionally.
    commands = dbops.CommandGroup()
    commands.add_command(
        dbops.CreateSchema(name=ext_schema, conditional=True),
    )
    if (
        inst_params.existing_exts is None
        or inst_params.existing_exts.get("uuid-ossp") is None
    ):
        commands.add_commands([
            dbops.CreateExtension(
                dbops.Extension(name='uuid-ossp', schema=ext_schema),
            ),
        ])
    block = dbops.PLTopBlock()
    commands.generate(block)
    await _execute_block(conn, block)


async def patch_pg_extensions(
    conn: PGConnection,
    backend_params: params.BackendRuntimeParams,
) -> None:
    # A single database backend might restrict creation of extensions
    # to a specific schema, or restrict creation of extensions altogether
    # and provide a way to register them using a different method
    # (e.g. a hosting panel UI).
    inst_params = backend_params.instance_params
    if inst_params.existing_exts is not None:
        uuid_ext_schema = inst_params.existing_exts.get("uuid-ossp")
        if uuid_ext_schema is None:
            uuid_ext_schema = inst_params.ext_schema
    else:
        uuid_ext_schema = inst_params.ext_schema

    commands = dbops.CommandGroup()

    if uuid_ext_schema != "edgedbext":
        commands.add_commands([
            dbops.CreateFunction(
                UuidGenerateV1mcFunction(uuid_ext_schema), or_replace=True),
            dbops.CreateFunction(
                UuidGenerateV4Function(uuid_ext_schema), or_replace=True),
            dbops.CreateFunction(
                UuidGenerateV5Function(uuid_ext_schema), or_replace=True),
        ])

    if len(commands) > 0:
        block = dbops.PLTopBlock()
        commands.generate(block)
        await _execute_block(conn, block)


classref_attr_aliases = {
    'links': 'pointers',
    'link_properties': 'pointers'
}


def tabname(
    schema: s_schema.Schema, obj: s_obj.QualifiedObject
) -> tuple[str, str]:
    return common.get_backend_name(
        schema,
        obj,
        aspect='table',
        catenate=False,
    )


def inhviewname(
    schema: s_schema.Schema, obj: s_obj.QualifiedObject
) -> Tuple[str, str]:
    return common.get_backend_name(
        schema,
        obj,
        aspect='inhview',
        catenate=False,
    )


def ptr_col_name(
    schema: s_schema.Schema,
    obj: s_sources.Source,
    propname: str,
) -> str:
    prop = obj.getptr(schema, s_name.UnqualName(propname))
    psi = types.get_pointer_storage_info(prop, schema=schema)
    return psi.column_name


def format_fields(
    schema: s_schema.Schema,
    obj: s_sources.Source,
    fields: dict[str, str],
) -> str:
    """Format a dictionary of column mappings for database views

    The reason we do it this way is because, since these views are
    overwriting existing temporary views, we need to put all the
    columns in the same order as the original view.
    """
    ptrs = [obj.getptr(schema, s_name.UnqualName(s)) for s in fields]

    # Sort by the order the pointers were added to the source.
    # N.B: This only works because we are using the original in-memory
    # schema. If it was loaded from reflection it probably wouldn't
    # work.
    ptr_indexes = {
        v: i for i, v in enumerate(obj.get_pointers(schema).objects(schema))
    }
    ptrs.sort(key=(
        lambda p: (not p.is_link_source_property(schema), ptr_indexes[p])
    ))

    cols = []
    for ptr in ptrs:
        name = ptr.get_shortname(schema).name
        val = fields[name]
        sname = qi(ptr_col_name(schema, obj, name))
        cols.append(f'            {val} AS {sname}')

    return ',\n'.join(cols)


def _generate_database_views(schema: s_schema.Schema) -> List[dbops.View]:
    Database = schema.get('sys::Database', type=s_objtypes.ObjectType)
    annos = Database.getptr(
        schema, s_name.UnqualName('annotations'), type=s_links.Link)
    int_annos = Database.getptr(
        schema, s_name.UnqualName('annotations__internal'), type=s_links.Link)

    view_fields = {
        'id': "((d.description)->>'id')::uuid",
        'internal': f"""(CASE WHEN
                (edgedb.get_backend_capabilities()
                 & {int(params.BackendCapabilities.CREATE_DATABASE)}) != 0
             THEN
                datname IN (
                    edgedb.get_database_backend_name(
                        {ql(defines.EDGEDB_TEMPLATE_DB)}),
                    edgedb.get_database_backend_name(
                        {ql(defines.EDGEDB_SYSTEM_DB)})
                )
             ELSE False END
        )""",
        'name': "(d.description)->>'name'",
        'name__internal': "(d.description)->>'name'",
        'computed_fields': 'ARRAY[]::text[]',
        'builtin': "((d.description)->>'builtin')::bool",
    }

    view_query = f'''
        SELECT
            {format_fields(schema, Database, view_fields)}
        FROM
            pg_database dat
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(dat.oid, 'pg_database')
                        AS description
            ) AS d
        WHERE
            (d.description)->>'id' IS NOT NULL
            AND (d.description)->>'tenant_id' = edgedb.get_backend_tenant_id()
    '''

    annos_link_fields = {
        'source': "((d.description)->>'id')::uuid",
        'target': "(annotations->>'id')::uuid",
        'value': "(annotations->>'value')::text",
        'owned': "(annotations->>'owned')::bool",
    }

    annos_link_query = f'''
        SELECT
            {format_fields(schema, annos, annos_link_fields)}
        FROM
            pg_database dat
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(dat.oid, 'pg_database')
                        AS description
            ) AS d
            CROSS JOIN LATERAL
                ROWS FROM (
                    jsonb_array_elements((d.description)->'annotations')
                ) AS annotations
    '''

    int_annos_link_fields = {
        'source': "((d.description)->>'id')::uuid",
        'target': "(annotations->>'id')::uuid",
        'owned': "(annotations->>'owned')::bool",
    }

    int_annos_link_query = f'''
        SELECT
            {format_fields(schema, int_annos, int_annos_link_fields)}
        FROM
            pg_database dat
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(dat.oid, 'pg_database')
                        AS description
            ) AS d
            CROSS JOIN LATERAL
                ROWS FROM (
                    jsonb_array_elements(
                        (d.description)->'annotations__internal'
                    )
                ) AS annotations
    '''

    objects = {
        Database: view_query,
        annos: annos_link_query,
        int_annos: int_annos_link_query,
    }

    views = []
    for obj, query in objects.items():
        tabview = dbops.View(name=tabname(schema, obj), query=query)
        views.append(tabview)

    return views


def _generate_extension_views(schema: s_schema.Schema) -> List[dbops.View]:
    ExtPkg = schema.get('sys::ExtensionPackage', type=s_objtypes.ObjectType)
    annos = ExtPkg.getptr(
        schema, s_name.UnqualName('annotations'), type=s_links.Link)
    int_annos = ExtPkg.getptr(
        schema, s_name.UnqualName('annotations__internal'), type=s_links.Link)
    ver = ExtPkg.getptr(
        schema, s_name.UnqualName('version'), type=s_props.Property)
    ver_t = common.get_backend_name(
        schema,
        not_none(ver.get_target(schema)),
        catenate=False,
    )

    view_query_fields = {
        'id': "(e.value->>'id')::uuid",
        'name': "(e.value->>'name')",
        'name__internal': "(e.value->>'name__internal')",
        'script': "(e.value->>'script')",
        'sql_extensions': '''
            COALESCE(
                (SELECT array_agg(edgedb.jsonb_extract_scalar(q.v, 'string'))
                FROM jsonb_array_elements(
                    e.value->'sql_extensions'
                ) AS q(v)),
                ARRAY[]::text[]
            )
        ''',
        'dependencies': '''
            COALESCE(
                (SELECT array_agg(edgedb.jsonb_extract_scalar(q.v, 'string'))
                FROM jsonb_array_elements(
                    e.value->'dependencies'
                ) AS q(v)),
                ARRAY[]::text[]
            )
        ''',
        'ext_module': "(e.value->>'ext_module')",
        'computed_fields': 'ARRAY[]::text[]',
        'builtin': "(e.value->>'builtin')::bool",
        'internal': "(e.value->>'internal')::bool",
        'version': f'''
            (
                (e.value->'version'->>'major')::int,
                (e.value->'version'->>'minor')::int,
                (e.value->'version'->>'stage')::text,
                (e.value->'version'->>'stage_no')::int,
                COALESCE(
                    (SELECT array_agg(q.v::text)
                    FROM jsonb_array_elements(
                        e.value->'version'->'local'
                    ) AS q(v)),
                    ARRAY[]::text[]
                )
            )::{qt(ver_t)}
        ''',
    }

    view_query = f'''
        SELECT
            {format_fields(schema, ExtPkg, view_query_fields)}
        FROM
            jsonb_each(
                edgedb.get_database_metadata(
                    {ql(defines.EDGEDB_TEMPLATE_DB)}
                ) -> 'ExtensionPackage'
            ) AS e
    '''

    annos_link_fields = {
        'source': "(e.value->>'id')::uuid",
        'target': "(annotations->>'id')::uuid",
        'value': "(annotations->>'value')::text",
        'owned': "(annotations->>'owned')::bool",
    }

    int_annos_link_fields = {
        'source': "(e.value->>'id')::uuid",
        'target': "(annotations->>'id')::uuid",
        'owned': "(annotations->>'owned')::bool",
    }

    annos_link_query = f'''
        SELECT
            {format_fields(schema, annos, annos_link_fields)}
        FROM
            jsonb_each(
                edgedb.get_database_metadata(
                    {ql(defines.EDGEDB_TEMPLATE_DB)}
                ) -> 'ExtensionPackage'
            ) AS e
            CROSS JOIN LATERAL
                ROWS FROM (
                    jsonb_array_elements(e.value->'annotations')
                ) AS annotations
    '''

    int_annos_link_query = f'''
        SELECT
            {format_fields(schema, int_annos, int_annos_link_fields)}
        FROM
            jsonb_each(
                edgedb.get_database_metadata(
                    {ql(defines.EDGEDB_TEMPLATE_DB)}
                ) -> 'ExtensionPackage'
            ) AS e
            CROSS JOIN LATERAL
                ROWS FROM (
                    jsonb_array_elements(e.value->'annotations__internal')
                ) AS annotations
    '''

    objects = {
        ExtPkg: view_query,
        annos: annos_link_query,
        int_annos: int_annos_link_query,
    }

    views = []
    for obj, query in objects.items():
        tabview = dbops.View(name=tabname(schema, obj), query=query)
        views.append(tabview)

    return views


def _generate_role_views(schema: s_schema.Schema) -> List[dbops.View]:
    Role = schema.get('sys::Role', type=s_objtypes.ObjectType)
    member_of = Role.getptr(
        schema, s_name.UnqualName('member_of'), type=s_links.Link)
    bases = Role.getptr(
        schema, s_name.UnqualName('bases'), type=s_links.Link)
    ancestors = Role.getptr(
        schema, s_name.UnqualName('ancestors'), type=s_links.Link)
    annos = Role.getptr(
        schema, s_name.UnqualName('annotations'), type=s_links.Link)
    int_annos = Role.getptr(
        schema, s_name.UnqualName('annotations__internal'), type=s_links.Link)

    superuser = f'''
        a.rolsuper OR EXISTS (
            SELECT
            FROM
                pg_auth_members m
                INNER JOIN pg_catalog.pg_roles g
                    ON (m.roleid = g.oid)
            WHERE
                m.member = a.oid
                AND g.rolname = edgedb.get_role_backend_name(
                    {ql(defines.EDGEDB_SUPERGROUP)}
                )
        )
    '''

    view_query_fields = {
        'id': "((d.description)->>'id')::uuid",
        'name': "(d.description)->>'name'",
        'name__internal': "(d.description)->>'name'",
        'superuser': f'{superuser}',
        'abstract': 'False',
        'is_derived': 'False',
        'inherited_fields': 'ARRAY[]::text[]',
        'computed_fields': 'ARRAY[]::text[]',
        'builtin': "((d.description)->>'builtin')::bool",
        'internal': 'False',
        'password': "(d.description)->>'password_hash'",
    }

    view_query = f'''
        SELECT
            {format_fields(schema, Role, view_query_fields)}
        FROM
            pg_catalog.pg_roles AS a
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(a.oid, 'pg_authid')
                        AS description
            ) AS d
        WHERE
            (d.description)->>'id' IS NOT NULL
            AND (d.description)->>'tenant_id' = edgedb.get_backend_tenant_id()
    '''

    member_of_link_query_fields = {
        'source': "((d.description)->>'id')::uuid",
        'target': "((md.description)->>'id')::uuid",
    }

    member_of_link_query = f'''
        SELECT
            {format_fields(schema, member_of, member_of_link_query_fields)}
        FROM
            pg_catalog.pg_roles AS a
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(a.oid, 'pg_authid')
                        AS description
            ) AS d
            INNER JOIN pg_auth_members m ON m.member = a.oid
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(m.roleid, 'pg_authid')
                        AS description
            ) AS md
    '''

    bases_link_query_fields = {
        'source': "((d.description)->>'id')::uuid",
        'target': "((md.description)->>'id')::uuid",
        'index': 'row_number() OVER (PARTITION BY a.oid ORDER BY m.roleid)',
    }

    bases_link_query = f'''
        SELECT
            {format_fields(schema, bases, bases_link_query_fields)}
        FROM
            pg_catalog.pg_roles AS a
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(a.oid, 'pg_authid')
                        AS description
            ) AS d
            INNER JOIN pg_auth_members m ON m.member = a.oid
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(m.roleid, 'pg_authid')
                        AS description
            ) AS md
    '''

    ancestors_link_query = f'''
        SELECT
            {format_fields(schema, ancestors, bases_link_query_fields)}
        FROM
            pg_catalog.pg_roles AS a
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(a.oid, 'pg_authid')
                        AS description
            ) AS d
            INNER JOIN pg_auth_members m ON m.member = a.oid
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(m.roleid, 'pg_authid')
                        AS description
            ) AS md
    '''

    annos_link_fields = {
        'source': "((d.description)->>'id')::uuid",
        'target': "(annotations->>'id')::uuid",
        'value': "(annotations->>'value')::text",
        'owned': "(annotations->>'owned')::bool",
    }

    annos_link_query = f'''
        SELECT
            {format_fields(schema, annos, annos_link_fields)}
        FROM
            pg_catalog.pg_roles AS a
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(a.oid, 'pg_authid')
                        AS description
            ) AS d
            CROSS JOIN LATERAL
                ROWS FROM (
                    jsonb_array_elements(
                        (d.description)->'annotations'
                    )
                ) AS annotations
    '''

    int_annos_link_fields = {
        'source': "((d.description)->>'id')::uuid",
        'target': "(annotations->>'id')::uuid",
        'owned': "(annotations->>'owned')::bool",
    }

    int_annos_link_query = f'''
        SELECT
            {format_fields(schema, int_annos, int_annos_link_fields)}
        FROM
            pg_catalog.pg_roles AS a
            CROSS JOIN LATERAL (
                SELECT
                    edgedb.shobj_metadata(a.oid, 'pg_authid')
                        AS description
            ) AS d
            CROSS JOIN LATERAL
                ROWS FROM (
                    jsonb_array_elements(
                        (d.description)->'annotations__internal'
                    )
                ) AS annotations
    '''

    objects = {
        Role: view_query,
        member_of: member_of_link_query,
        bases: bases_link_query,
        ancestors: ancestors_link_query,
        annos: annos_link_query,
        int_annos: int_annos_link_query,
    }

    views = []
    for obj, query in objects.items():
        tabview = dbops.View(name=tabname(schema, obj), query=query)
        views.append(tabview)

    return views


def _generate_single_role_views(schema: s_schema.Schema) -> List[dbops.View]:
    Role = schema.get('sys::Role', type=s_objtypes.ObjectType)
    member_of = Role.getptr(
        schema, s_name.UnqualName('member_of'), type=s_links.Link)
    bases = Role.getptr(
        schema, s_name.UnqualName('bases'), type=s_links.Link)
    ancestors = Role.getptr(
        schema, s_name.UnqualName('ancestors'), type=s_links.Link)
    annos = Role.getptr(
        schema, s_name.UnqualName('annotations'), type=s_links.Link)
    int_annos = Role.getptr(
        schema, s_name.UnqualName('annotations__internal'), type=s_links.Link)
    view_query_fields = {
        'id': "(json->>'id')::uuid",
        'name': "json->>'name'",
        'name__internal': "json->>'name'",
        'superuser': 'True',
        'abstract': 'False',
        'is_derived': 'False',
        'inherited_fields': 'ARRAY[]::text[]',
        'computed_fields': 'ARRAY[]::text[]',
        'builtin': 'True',
        'internal': 'False',
        'password': "json->>'password_hash'",
    }

    view_query = f'''
        SELECT
            {format_fields(schema, Role, view_query_fields)}
        FROM
            edgedbinstdata.instdata
        WHERE
            key = 'single_role_metadata'
            AND json->>'tenant_id' = edgedb.get_backend_tenant_id()
    '''

    member_of_link_query_fields = {
        'source': "'00000000-0000-0000-0000-000000000000'::uuid",
        'target': "'00000000-0000-0000-0000-000000000000'::uuid",
    }

    member_of_link_query = f'''
        SELECT
            {format_fields(schema, member_of, member_of_link_query_fields)}
        LIMIT 0
    '''

    bases_link_query_fields = {
        'source': "'00000000-0000-0000-0000-000000000000'::uuid",
        'target': "'00000000-0000-0000-0000-000000000000'::uuid",
        'index': "0::bigint",
    }

    bases_link_query = f'''
        SELECT
            {format_fields(schema, bases, bases_link_query_fields)}
        LIMIT 0
    '''

    ancestors_link_query = f'''
        SELECT
            {format_fields(schema, ancestors, bases_link_query_fields)}
        LIMIT 0
    '''

    annos_link_fields = {
        'source': "(json->>'id')::uuid",
        'target': "(annotations->>'id')::uuid",
        'value': "(annotations->>'value')::text",
        'owned': "(annotations->>'owned')::bool",
    }

    annos_link_query = f'''
        SELECT
            {format_fields(schema, annos, annos_link_fields)}
        FROM
            edgedbinstdata.instdata
            CROSS JOIN LATERAL
                ROWS FROM (
                    jsonb_array_elements(json->'annotations')
                ) AS annotations
        WHERE
            key = 'single_role_metadata'
            AND json->>'tenant_id' = edgedb.get_backend_tenant_id()
    '''

    int_annos_link_fields = {
        'source': "(json->>'id')::uuid",
        'target': "(annotations->>'id')::uuid",
        'owned': "(annotations->>'owned')::bool",
    }

    int_annos_link_query = f'''
        SELECT
            {format_fields(schema, int_annos, int_annos_link_fields)}
        FROM
            edgedbinstdata.instdata
            CROSS JOIN LATERAL
                ROWS FROM (
                    jsonb_array_elements(json->'annotations__internal')
                ) AS annotations
        WHERE
            key = 'single_role_metadata'
            AND json->>'tenant_id' = edgedb.get_backend_tenant_id()
    '''

    objects = {
        Role: view_query,
        member_of: member_of_link_query,
        bases: bases_link_query,
        ancestors: ancestors_link_query,
        annos: annos_link_query,
        int_annos: int_annos_link_query,
    }

    views = []
    for obj, query in objects.items():
        tabview = dbops.View(name=tabname(schema, obj), query=query)
        views.append(tabview)

    return views


def _generate_schema_ver_views(schema: s_schema.Schema) -> List[dbops.View]:
    Ver = schema.get(
        'sys::GlobalSchemaVersion',
        type=s_objtypes.ObjectType,
    )

    view_fields = {
        'id': "(v.value->>'id')::uuid",
        'name': "(v.value->>'name')",
        'name__internal': "(v.value->>'name')",
        'version': "(v.value->>'version')::uuid",
        'builtin': "(v.value->>'builtin')::bool",
        'internal': "(v.value->>'internal')::bool",
        'computed_fields': 'ARRAY[]::text[]',
    }

    view_query = f'''
        SELECT
            {format_fields(schema, Ver, view_fields)}
        FROM
            jsonb_each(
                edgedb.get_database_metadata(
                    {ql(defines.EDGEDB_TEMPLATE_DB)}
                ) -> 'GlobalSchemaVersion'
            ) AS v
    '''

    objects = {
        Ver: view_query
    }

    views = []
    for obj, query in objects.items():
        tabview = dbops.View(name=tabname(schema, obj), query=query)
        views.append(tabview)

    return views


def _make_json_caster(
    schema: s_schema.Schema,
    stype: s_types.Type,
) -> Callable[[str], str]:
    cast_expr = qlast.TypeCast(
        expr=qlast.TypeCast(
            expr=qlast.Parameter(name="__replaceme__"),
            type=s_utils.typeref_to_ast(schema, schema.get('std::json')),
        ),
        type=s_utils.typeref_to_ast(schema, stype),
    )

    cast_ir = qlcompiler.compile_ast_fragment_to_ir(
        cast_expr,
        schema,
    )

    cast_sql_res = compiler.compile_ir_to_sql_tree(
        cast_ir,
        named_param_prefix=(),
        singleton_mode=True,
    )
    cast_sql = codegen.generate_source(cast_sql_res.ast)

    return lambda val: cast_sql.replace('__replaceme__', val)


def _generate_schema_alias_views(
    schema: s_schema.Schema,
    module: s_name.UnqualName,
) -> List[dbops.View]:
    views = []

    schema_objs = schema.get_objects(
        type=s_objtypes.ObjectType,
        included_modules=(module,),
    )

    for schema_obj in schema_objs:
        views.append(_generate_schema_alias_view(schema, schema_obj))

    return views


def _generate_schema_alias_view(
    schema: s_schema.Schema,
    obj: s_sources.Source,
) -> dbops.View:

    module = obj.get_name(schema).module
    bn = common.get_backend_name(
        schema,
        obj,
        aspect='inhview',
        catenate=False,
    )

    targets = []

    if isinstance(obj, s_links.Link):
        expected_tt = "link"
    else:
        expected_tt = "ObjectType"

    for ptr in obj.get_pointers(schema).objects(schema):
        if ptr.is_pure_computable(schema):
            continue
        psi = types.get_pointer_storage_info(ptr, schema=schema)
        if psi.table_type == expected_tt:
            ptr_name = ptr.get_shortname(schema).name
            col_name = psi.column_name
            if col_name == '__type__':
                val = f'{ql(str(obj.id))}::uuid'
            else:
                val = f'{qi(col_name)}'

            if col_name != ptr_name:
                targets.append(f'{val} AS {qi(ptr_name)}')
            targets.append(f'{val} AS {qi(col_name)}')

    prefix = module.capitalize()

    if isinstance(obj, s_links.Link):
        objtype = obj.get_source(schema)
        assert objtype is not None
        objname = objtype.get_name(schema).name
        lname = obj.get_shortname(schema).name
        name = f'_{prefix}{objname}__{lname}'
    else:
        name = f'_{prefix}{obj.get_name(schema).name}'

    return dbops.View(
        name=('edgedb', name),
        query=(f'SELECT {", ".join(targets)} FROM {q(*bn)}')
    )


def _generate_sql_information_schema() -> List[dbops.Command]:

    system_columns = ['tableoid', 'xmin', 'cmin', 'xmax', 'cmax', 'ctid']

    # A helper view that contains all data tables we expose over SQL, excluding
    # introspection tables.
    # It contains table & schema names and associated module id.
    virtual_tables = dbops.View(
        name=('edgedbsql', 'virtual_tables'),
        query='''
        WITH obj_ty_pre AS (
            SELECT
                id,
                REGEXP_REPLACE(name, '::[^:]*$', '') AS module_name,
                REGEXP_REPLACE(name, '^.*::', '') as table_name
            FROM edgedb."_SchemaObjectType"
            WHERE internal IS NOT TRUE
        ),
        obj_ty AS (
            SELECT
                id,
                REGEXP_REPLACE(module_name, '^default(?=::|$)', 'public')
                    AS schema_name,
                module_name,
                table_name
            FROM obj_ty_pre
        ),
        all_tables (id, schema_name, module_name, table_name) AS ((
            SELECT * FROM obj_ty
        ) UNION ALL (
            WITH qualified_links AS (
                -- multi links and links with at least one property
                -- (besides source and target)
                SELECT link.id
                FROM edgedb."_SchemaLink" link
                JOIN edgedb."_SchemaProperty" AS prop ON link.id = prop.source
                WHERE prop.computable IS NOT TRUE AND prop.internal IS NOT TRUE
                GROUP BY link.id, link.cardinality
                HAVING link.cardinality = 'Many' OR COUNT(*) > 2
            )
            SELECT link.id, obj_ty.schema_name, obj_ty.module_name,
                CONCAT(obj_ty.table_name, '.', link.name) AS table_name
            FROM edgedb."_SchemaLink" link
            JOIN obj_ty ON obj_ty.id = link.source
            WHERE link.id IN (SELECT * FROM qualified_links)
        ) UNION ALL (
            -- multi properties
            SELECT prop.id, obj_ty.schema_name, obj_ty.module_name,
                CONCAT(obj_ty.table_name, '.', prop.name) AS table_name
            FROM edgedb."_SchemaProperty" AS prop
            JOIN obj_ty ON obj_ty.id = prop.source
            WHERE prop.computable IS NOT TRUE
            AND prop.internal IS NOT TRUE
            AND prop.cardinality = 'Many'
        ))
        SELECT
            at.id,
            schema_name,
            table_name,
            sm.id AS module_id,
            pt.oid AS backend_id
        FROM all_tables at
        JOIN edgedb."_SchemaModule" sm ON sm.name = at.module_name
        LEFT JOIN pg_type pt ON pt.typname = at.id::text
        WHERE schema_name not in ('cfg', 'sys', 'schema', 'std')
        '''
    )
    # A few tables in here were causing problems, so let's hide them as an
    # implementation detail.
    # To be more specific:
    # - following tables were missing from information_schema:
    #   Link.properties, ObjectType.links, ObjectType.properties
    # - even though introspection worked, I wasn't able to select from some
    #   tables in cfg and sys

    # For making up oids of schemas that represent modules
    uuid_to_oid = dbops.Function(
        name=('edgedbsql', 'uuid_to_oid'),
        args=(
            ('id', 'uuid'),
        ),
        returns=('oid',),
        volatility='immutable',
        text="""
            SELECT (
                ('x' || substring(id::text, 2, 7))::bit(28)::bigint
                 + 40000)::oid;
        """
    )
    long_name = dbops.Function(
        name=('edgedbsql', '_long_name'),
        args=[
            ('origname', ('text',)),
            ('longname', ('text',)),
        ],
        returns=('text',),
        volatility='stable',
        text=r'''
            SELECT CASE WHEN length(longname) > 63
                THEN left(longname, 55) || left(origname, 8)
                ELSE longname
                END
        '''
    )
    type_rename = dbops.Function(
        name=('edgedbsql', '_pg_type_rename'),
        args=[
            ('typeoid', ('oid',)),
            ('typename', ('name',)),
        ],
        returns=('name',),
        volatility='stable',
        text=r'''
            SELECT COALESCE (
                -- is the nmae in virtual_tables?
                (
                    SELECT vt.table_name::name
                    FROM edgedbsql.virtual_tables vt
                    WHERE vt.backend_id = typeoid
                ),
                -- is this a scalar or tuple?
                (
                    SELECT name::name
                    FROM (
                        -- get the built-in scalars
                        SELECT
                            split_part(name, '::', 2) AS name,
                            backend_id
                        FROM edgedb."_SchemaScalarType"
                        WHERE NOT builtin
                        UNION ALL
                        -- get the tuples
                        SELECT
                            edgedbsql._long_name(typename, name),
                            backend_id
                        FROM edgedb."_SchemaTuple"
                    ) x
                    WHERE x.backend_id = typeoid
                ),
                typename
            )
        '''
    )
    namespace_rename = dbops.Function(
        name=('edgedbsql', '_pg_namespace_rename'),
        args=[
            ('typeoid', ('oid',)),
            ('typens', ('oid',)),
        ],
        returns=('oid',),
        volatility='stable',
        text=r'''
            WITH
                nspub AS (
                    SELECT oid FROM pg_namespace WHERE nspname = 'edgedbpub'
                ),
                nsdef AS (
                    SELECT edgedbsql.uuid_to_oid(id) AS oid
                    FROM edgedb."_SchemaModule"
                    WHERE name = 'default'
                )
            SELECT COALESCE (
                (
                    SELECT edgedbsql.uuid_to_oid(vt.module_id)
                    FROM edgedbsql.virtual_tables vt
                    WHERE vt.backend_id = typeoid
                ),
                -- just replace "edgedbpub" with "public"
                (SELECT nsdef.oid WHERE typens = nspub.oid),
                typens
            )
            FROM
                nspub,
                nsdef
        '''
    )

    sql_ident = 'information_schema.sql_identifier'
    sql_str = 'information_schema.character_data'
    sql_bool = 'information_schema.yes_or_no'
    sql_card = 'information_schema.cardinal_number'
    tables_and_columns = [
        dbops.View(
            name=('edgedbsql', 'tables'),
            query=(
                f'''
        SELECT
            edgedb.get_current_database()::{sql_ident} AS table_catalog,
            vt.schema_name::{sql_ident} AS table_schema,
            vt.table_name::{sql_ident} AS table_name,
            ist.table_type,
            ist.self_referencing_column_name,
            ist.reference_generation,
            ist.user_defined_type_catalog,
            ist.user_defined_type_schema,
            ist.user_defined_type_name,
            ist.is_insertable_into,
            ist.is_typed,
            ist.commit_action
        FROM information_schema.tables ist
        JOIN edgedbsql.virtual_tables vt ON vt.id::text = ist.table_name
            '''
            ),
        ),
        dbops.View(
            name=('edgedbsql', 'columns'),
            query=(
                f'''
        SELECT
            edgedb.get_current_database()::{sql_ident} AS table_catalog,
            vt.schema_name::{sql_ident} AS table_schema,
            vt.table_name::{sql_ident} AS table_name,
            COALESCE(
                sp.name || case when sl.id is not null then '_id' else '' end,
                isc.column_name
            )::{sql_ident} AS column_name,
            ROW_NUMBER() OVER (
                PARTITION BY vt.schema_name, vt.table_name
                ORDER BY
                    CASE WHEN isc.column_name = 'id' THEN 0 ELSE 1 END,
                    COALESCE(sp.name, isc.column_name)
            ) AS ordinal_position,
            isc.column_default,
            isc.is_nullable,
            isc.data_type,
            NULL::{sql_card} AS character_maximum_length,
            NULL::{sql_card} AS character_octet_length,
            NULL::{sql_card} AS numeric_precision,
            NULL::{sql_card} AS numeric_precision_radix,
            NULL::{sql_card} AS numeric_scale,
            NULL::{sql_card} AS datetime_precision,
            NULL::{sql_str} AS interval_type,
            NULL::{sql_card} AS interval_precision,
            NULL::{sql_ident} AS character_set_catalog,
            NULL::{sql_ident} AS character_set_schema,
            NULL::{sql_ident} AS character_set_name,
            NULL::{sql_ident} AS collation_catalog,
            NULL::{sql_ident} AS collation_schema,
            NULL::{sql_ident} AS collation_name,
            NULL::{sql_ident} AS domain_catalog,
            NULL::{sql_ident} AS domain_schema,
            NULL::{sql_ident} AS domain_name,
            edgedb.get_current_database()::{sql_ident} AS udt_catalog,
            'pg_catalog'::{sql_ident} AS udt_schema,
            NULL::{sql_ident} AS udt_name,
            NULL::{sql_ident} AS scope_catalog,
            NULL::{sql_ident} AS scope_schema,
            NULL::{sql_ident} AS scope_name,
            NULL::{sql_card} AS maximum_cardinality,
            0::{sql_ident} AS dtd_identifier,
            'NO'::{sql_bool} AS is_self_referencing,
            'NO'::{sql_bool} AS is_identity,
            NULL::{sql_str} AS identity_generation,
            NULL::{sql_str} AS identity_start,
            NULL::{sql_str} AS identity_increment,
            NULL::{sql_str} AS identity_maximum,
            NULL::{sql_str} AS identity_minimum,
            'NO' ::{sql_bool} AS identity_cycle,
            'NEVER'::{sql_str} AS is_generated,
            NULL::{sql_str} AS generation_expression,
            'YES'::{sql_bool} AS is_updatable
        FROM information_schema.columns isc
        JOIN edgedbsql.virtual_tables vt ON vt.id::text = isc.table_name
        LEFT JOIN edgedb."_SchemaPointer" sp ON sp.id::text = isc.column_name
        LEFT JOIN edgedb."_SchemaLink" sl ON sl.id::text = isc.column_name
        WHERE column_name != '__type__'
            '''
            ),
        ),
    ]

    pg_catalog_views = [
        dbops.View(
            name=("edgedbsql", "pg_namespace"),
            query="""
        SELECT
            oid,
            nspname,
            nspowner,
            nspacl,
            tableoid,
            xmin,
            cmin,
            xmax,
            cmax,
            ctid
        FROM pg_namespace
        WHERE nspname IN ('pg_catalog', 'pg_toast', 'information_schema',
                          'edgedb', 'edgedbstd')
        UNION ALL
        SELECT
            edgedbsql.uuid_to_oid(t.module_id)  AS oid,
            t.schema_name                       AS nspname,
            (SELECT oid
             FROM pg_roles
             WHERE rolname = CURRENT_USER
             LIMIT 1)                           AS nspowner,
            NULL AS nspacl,
            (SELECT pg_class.oid
             FROM pg_class
             JOIN pg_namespace ON pg_class.relnamespace = pg_namespace.oid
             WHERE pg_namespace.nspname = 'pg_catalog'::name
             AND pg_class.relname = 'pg_namespace'::name
             )                                  AS tableoid,
            '0'::xid                            AS xmin,
            '0'::cid                            AS cmin,
            '0'::xid                            AS xmax,
            '0'::cid                            AS cmax,
            NULL                                AS ctid
        FROM (
            SELECT DISTINCT schema_name, module_id
            FROM edgedbsql.virtual_tables
        ) t
        """,
        ),
        dbops.View(
            name=("edgedbsql", "pg_type"),
            query="""
        SELECT
            pt.oid,
            edgedbsql._pg_type_rename(pt.oid, pt.typname)
                AS typname,
            edgedbsql._pg_namespace_rename(pt.oid, pt.typnamespace)
                AS typnamespace,
            {0},
            pt.tableoid, pt.xmin, pt.cmin, pt.xmax, pt.cmax, pt.ctid
        FROM pg_type pt
        JOIN pg_namespace pn ON pt.typnamespace = pn.oid
        WHERE
            nspname IN ('pg_catalog', 'pg_toast', 'information_schema',
                        'edgedb', 'edgedbstd', 'edgedbpub')
        """.format(
                ",".join(
                    f"pt.{col}"
                    for col, _ in sql_introspection.PG_CATALOG["pg_type"][3:]
                )
            ),
        ),
        # TODO: Should we try to filter here, and fix up some stuff
        # elsewhere, instead of overriding pg_get_constraintdef?
        dbops.View(
            name=("edgedbsql", "pg_constraint"),
            query="""
        SELECT
            pc.*,
            pc.tableoid, pc.xmin, pc.cmin, pc.xmax, pc.cmax, pc.ctid
        FROM pg_constraint pc
        JOIN pg_namespace pn ON pc.connamespace = pn.oid
        WHERE NOT (pn.nspname = 'edgedbpub' AND pc.conbin IS NOT NULL)
        """
        ),
        dbops.View(
            name=("edgedbsql", "pg_index"),
            query="""
        SELECT pi.*, pi.tableoid, pi.xmin, pi.cmin, pi.xmax, pi.cmax, pi.ctid
        FROM pg_index pi
        LEFT JOIN pg_class pr ON pi.indrelid = pr.oid
        LEFT JOIN pg_catalog.pg_namespace pn ON pr.relnamespace = pn.oid
        WHERE pn.nspname <> 'edgedbpub'
        """,
        ),
        dbops.View(
            name=("edgedbsql", "pg_class"),
            query="""
        WITH
            nsdef AS (
                SELECT edgedbsql.uuid_to_oid(id) AS oid
                FROM edgedb."_SchemaModule"
                WHERE name = 'default'
            )
        -- Postgres tables
        SELECT pc.*, pc.tableoid, pc.xmin, pc.cmin, pc.xmax, pc.cmax, pc.ctid
        FROM pg_class pc
        JOIN pg_namespace pn ON pc.relnamespace = pn.oid
        WHERE nspname IN ('pg_catalog', 'pg_toast', 'information_schema')

        UNION ALL

        -- get all the tuples
        SELECT
            pc.oid,
            edgedbsql._long_name(pc.reltype::text, tup.name) as relname,
            nsdef.oid as relnamespace,
            pc.reltype,
            pc.reloftype,
            pc.relowner,
            pc.relam,
            pc.relfilenode,
            pc.reltablespace,
            pc.relpages,
            pc.reltuples,
            pc.relallvisible,
            pc.reltoastrelid,
            pc.relhasindex,
            pc.relisshared,
            pc.relpersistence,
            pc.relkind,
            pc.relnatts,
            0 as relchecks, -- don't care about CHECK constraints
            pc.relhasrules,
            pc.relhastriggers,
            pc.relhassubclass,
            pc.relrowsecurity,
            pc.relforcerowsecurity,
            pc.relispopulated,
            pc.relreplident,
            pc.relispartition,
            pc.relrewrite,
            pc.relfrozenxid,
            pc.relminmxid,
            pc.relacl,
            pc.reloptions,
            pc.relpartbound,
            pc.tableoid,
            pc.xmin,
            pc.cmin,
            pc.xmax,
            pc.cmax,
            pc.ctid
        FROM
            nsdef,
            pg_class pc
        JOIN edgedb."_SchemaTuple" tup ON tup.backend_id = pc.reltype

        UNION ALL

        -- user-defined tables
        SELECT
            oid,
            vt.table_name as relname,
            edgedbsql.uuid_to_oid(vt.module_id) as relnamespace,
            reltype,
            reloftype,
            relowner,
            relam,
            relfilenode,
            reltablespace,
            relpages,
            reltuples,
            relallvisible,
            reltoastrelid,
            relhasindex,
            relisshared,
            relpersistence,
            relkind,
            relnatts,
            0 as relchecks, -- don't care about CHECK constraints
            relhasrules,
            relhastriggers,
            relhassubclass,
            relrowsecurity,
            relforcerowsecurity,
            relispopulated,
            relreplident,
            relispartition,
            relrewrite,
            relfrozenxid,
            relminmxid,
            relacl,
            reloptions,
            relpartbound,
            pc.tableoid,
            pc.xmin,
            pc.cmin,
            pc.xmax,
            pc.cmax,
            pc.ctid
        FROM pg_class pc
        JOIN edgedbsql.virtual_tables vt ON vt.backend_id = pc.reltype

        UNION

        -- indexes
        SELECT pc.*, pc.tableoid, pc.xmin, pc.cmin, pc.xmax, pc.cmax, pc.ctid
        FROM pg_class pc
        JOIN edgedbsql.pg_index pi ON pc.oid = pi.indexrelid
        """,
        ),
        dbops.View(
            name=("edgedbsql", "pg_attribute"),
            query="""
        SELECT attrelid,
            attname,
            atttypid,
            attstattarget,
            attlen,
            attnum,
            attndims,
            attcacheoff,
            atttypmod,
            attbyval,
            attstorage,
            attalign,
            attnotnull,
            atthasdef,
            atthasmissing,
            attidentity,
            attgenerated,
            attisdropped,
            attislocal,
            attinhcount,
            attcollation,
            attacl,
            attoptions,
            attfdwoptions,
            null::int[] as attmissingval,
            pa.tableoid,
            pa.xmin,
            pa.cmin,
            pa.xmax,
            pa.cmax,
            pa.ctid
        FROM pg_attribute pa
        JOIN pg_class pc ON pa.attrelid = pc.oid
        JOIN pg_namespace pn ON pc.relnamespace = pn.oid
        LEFT JOIN edgedb."_SchemaTuple" tup ON tup.backend_id = pc.reltype
        WHERE
            nspname IN ('pg_catalog', 'pg_toast', 'information_schema')
            OR
            tup.backend_id IS NOT NULL
        UNION ALL
        SELECT attrelid,
            COALESCE(
                sp.name || case when sl.id is not null then '_id' else '' end,
                pa.attname
            ) AS attname,
            atttypid,
            attstattarget,
            attlen,
            attnum,
            attndims,
            attcacheoff,
            atttypmod,
            attbyval,
            attstorage,
            attalign,
            attnotnull,
            -- Always report no default, to avoid expr trouble
            false as atthasdef,
            atthasmissing,
            attidentity,
            attgenerated,
            attisdropped,
            attislocal,
            attinhcount,
            attcollation,
            attacl,
            attoptions,
            attfdwoptions,
            null::int[] as attmissingval,
            pa.tableoid,
            pa.xmin,
            pa.cmin,
            pa.xmax,
            pa.cmax,
            pa.ctid
        FROM pg_attribute pa
        JOIN pg_class pc ON pc.oid = pa.attrelid
        JOIN edgedbsql.virtual_tables vt ON vt.backend_id = pc.reltype
        LEFT JOIN edgedb."_SchemaPointer" sp ON sp.id::text = pa.attname
        LEFT JOIN edgedb."_SchemaLink" sl ON sl.id::text = pa.attname
        WHERE pa.attname NOT IN ('__type__')
        """,
        ),
        dbops.View(
            name=("edgedbsql", "pg_database"),
            query="""
        SELECT
            oid,
            edgedb.get_current_database()::name as datname,
            datdba,
            encoding,
            datcollate,
            datctype,
            datistemplate,
            datallowconn,
            datconnlimit,
            0::oid AS datlastsysoid,
            datfrozenxid,
            datminmxid,
            dattablespace,
            datacl,
            tableoid, xmin, cmin, xmax, cmax, ctid
        FROM pg_database
        WHERE datname LIKE '%_edgedb'
        """,
        ),

        # HACK: there were problems with pg_dump when exposing this table, so
        # I've added WHERE FALSE. The query could be simplified, but it may
        # be needed in the future. Its EXPLAIN cost is 0..0 anyway.
        dbops.View(
            name=("edgedbsql", "pg_stats"),
            query="""
        SELECT n.nspname AS schemaname,
            c.relname AS tablename,
            a.attname,
            s.stainherit AS inherited,
            s.stanullfrac AS null_frac,
            s.stawidth AS avg_width,
            s.stadistinct AS n_distinct,
            NULL::real[] AS most_common_vals,
            s.stanumbers1 AS most_common_freqs,
            s.stanumbers1 AS histogram_bounds,
            s.stanumbers1[1] AS correlation,
            NULL::real[] AS most_common_elems,
            s.stanumbers1 AS most_common_elem_freqs,
            s.stanumbers1 AS elem_count_histogram
        FROM pg_statistic s
        JOIN pg_class c ON c.oid = s.starelid
        JOIN pg_attribute a ON c.oid = a.attrelid and a.attnum = s.staattnum
        LEFT JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE FALSE
        """,
        ),
        dbops.View(
            name=("edgedbsql", "pg_statistic"),
            query="""
        SELECT
            starelid,
            staattnum,
            stainherit,
            stanullfrac,
            stawidth,
            stadistinct,
            stakind1,
            stakind2,
            stakind3,
            stakind4,
            stakind5,
            staop1,
            staop2,
            staop3,
            staop4,
            staop5,
            stacoll1,
            stacoll2,
            stacoll3,
            stacoll4,
            stacoll5,
            stanumbers1,
            stanumbers2,
            stanumbers3,
            stanumbers4,
            stanumbers5,
            NULL::real[] AS stavalues1,
            NULL::real[] AS stavalues2,
            NULL::real[] AS stavalues3,
            NULL::real[] AS stavalues4,
            NULL::real[] AS stavalues5,
            tableoid, xmin, cmin, xmax, cmax, ctid
        FROM pg_statistic
        """,
        ),
        dbops.View(
            name=("edgedbsql", "pg_statistic_ext"),
            query="""
        SELECT
            oid,
            stxrelid,
            stxname,
            stxnamespace,
            stxowner,
            stxstattarget,
            stxkeys,
            stxkind,
            NULL::pg_node_tree as stxexprs,
            tableoid, xmin, cmin, xmax, cmax, ctid
        FROM pg_statistic_ext
        """,
        ),
        dbops.View(
            name=("edgedbsql", "pg_statistic_ext_data"),
            query="""
        SELECT
            stxoid,
            stxdndistinct,
            stxddependencies,
            stxdmcv,
            NULL::oid AS stxdexpr,
            tableoid, xmin, cmin, xmax, cmax, ctid
        FROM pg_statistic_ext_data
        """,
        ),
        dbops.View(
            name=("edgedbsql", "pg_rewrite"),
            query="""
        SELECT pr.*, pr.tableoid, pr.xmin, pr.cmin, pr.xmax, pr.cmax, pr.ctid
        FROM pg_rewrite pr
        JOIN edgedbsql.pg_class pn ON pr.ev_class = pn.oid
        """,
        ),

        # HACK: Automatically generated cast function for ranges/multiranges
        # was causing issues for pg_dump. So at the end of the day we opt for
        # not exposing any casts at all here since there is no real reason for
        # this compatibility layer that is read-only to have elaborate casts
        # present.
        dbops.View(
            name=("edgedbsql", "pg_cast"),
            query="""
        SELECT pc.*, pc.tableoid, pc.xmin, pc.cmin, pc.xmax, pc.cmax, pc.ctid
        FROM pg_cast pc
        WHERE FALSE
        """,
        ),
        # Omit all funcitons for now.
        dbops.View(
            name=("edgedbsql", "pg_proc"),
            query="""
        SELECT *, tableoid, xmin, cmin, xmax, cmax, ctid
        FROM pg_proc
        WHERE FALSE
        """,
        ),
        # Omit all operators for now.
        dbops.View(
            name=("edgedbsql", "pg_operator"),
            query="""
        SELECT *, tableoid, xmin, cmin, xmax, cmax, ctid
        FROM pg_operator
        WHERE FALSE
        """,
        ),
        # Omit all triggers for now.
        dbops.View(
            name=("edgedbsql", "pg_trigger"),
            query="""
        SELECT *, tableoid, xmin, cmin, xmax, cmax, ctid
        FROM pg_trigger
        WHERE FALSE
        """,
        ),
        # Omit all subscriptions for now.
        # This table is queried by pg_dump with COUNT(*) when user does not
        # have permissions to access it. This should be allowed, but the
        # view expands the query to all columns, which is not allowed.
        # So we have to construct an empty view with correct signature that
        # does not reference pg_subscription.
        dbops.View(
            name=("edgedbsql", "pg_subscription"),
            query="""
        SELECT
            NULL::oid AS oid,
            NULL::oid AS subdbid,
            NULL::name AS subname,
            NULL::oid AS subowner,
            NULL::boolean AS subenabled,
            NULL::text AS subconninfo,
            NULL::name AS subslotname,
            NULL::text AS subsynccommit,
            NULL::oid AS subpublications,
            tableoid, xmin, cmin, xmax, cmax, ctid
        FROM pg_namespace
        WHERE FALSE
        """,
        ),
    ]

    def construct_pg_view(table_name: str, columns: List[str]) -> dbops.View:
        if table_name in (
            'pg_aggregate',
            'pg_am',
            'pg_amop',
            'pg_amproc',
            'pg_attrdef',
            'pg_attribute',
            'pg_auth_members',
            'pg_authid',
            'pg_cast',
            'pg_class',
            'pg_collation',
            'pg_constraint',
            'pg_conversion',
            'pg_database',
            'pg_db_role_setting',
            'pg_default_acl',
            'pg_depend',
            'pg_description',
            'pg_enum',
            'pg_event_trigger',
            'pg_extension',
            'pg_foreign_data_wrapper',
            'pg_foreign_server',
            'pg_foreign_table',
            'pg_index',
            'pg_inherits',
            'pg_init_privs',
            'pg_language',
            'pg_largeobject',
            'pg_largeobject_metadata',
            'pg_namespace',
            'pg_opclass',
            'pg_operator',
            'pg_opfamily',
            'pg_partitioned_table',
            'pg_policy',
            'pg_publication',
            'pg_publication_rel',
            'pg_range',
            'pg_replication_origin',
            'pg_rewrite',
            'pg_seclabel',
            'pg_sequence',
            'pg_shdepend',
            'pg_shdescription',
            'pg_shseclabel',
            'pg_statistic',
            'pg_statistic_ext',
            'pg_statistic_ext_data',
            'pg_subscription_rel',
            'pg_tablespace',
            'pg_transform',
            'pg_trigger',
            'pg_ts_config',
            'pg_ts_config_map',
            'pg_ts_dict',
            'pg_ts_parser',
            'pg_ts_template',
            'pg_type',
            'pg_user_mapping',
        ):
            columns = list(columns) + system_columns

        columns_sql = ','.join('o.' + c for c in columns)
        return dbops.View(
            name=("edgedbsql", table_name),
            query=f"SELECT {columns_sql} FROM pg_catalog.{table_name} o",
        )

    # We expose most of the views as empty tables, just to prevent errors when
    # the tools do introspection.
    # For the tables that it turns out are actually needed, we handcraft the
    # views that expose the actual data.
    # I've been cautious about exposing too much data, for example limiting
    # pg_type to pg_catalog and pg_toast namespaces.
    views = []
    views.extend(tables_and_columns)

    for table_name, columns in sql_introspection.INFORMATION_SCHEMA.items():
        if table_name in ["tables", "columns"]:
            continue
        views.append(
            dbops.View(
                name=("edgedbsql", table_name),
                query="SELECT {} LIMIT 0".format(
                    ",".join(
                        f"NULL::information_schema.{type} AS {name}"
                        for name, type in columns
                    )
                ),
            )
        )

    views.extend(pg_catalog_views)

    for table_name, columns in sql_introspection.PG_CATALOG.items():
        if table_name in [
            'pg_type',
            'pg_attribute',
            'pg_namespace',
            'pg_class',
            'pg_database',
            'pg_proc',
            'pg_operator',
            'pg_pltemplate',
            'pg_stats',
            'pg_stats_ext_exprs',
            'pg_statistic',
            'pg_statistic_ext',
            'pg_statistic_ext_data',
            'pg_rewrite',
            'pg_cast',
            'pg_index',
            'pg_constraint',
            'pg_trigger',
            'pg_subscription',
        ]:
            continue

        views.append(construct_pg_view(table_name, [c for c, _ in columns]))

    util_functions = [
        dbops.Function(
            name=('edgedbsql', 'has_schema_privilege'),
            args=(
                ('schema_name', 'text'),
                ('privilege', 'text'),
            ),
            returns=('bool',),
            text="""
            SELECT COALESCE((
                SELECT has_schema_privilege(oid, privilege)
                FROM edgedbsql.pg_namespace
                WHERE nspname = schema_name
            ), TRUE);
            """
        ),
        dbops.Function(
            name=('edgedbsql', 'has_schema_privilege'),
            args=(
                ('schema_oid', 'oid'),
                ('privilege', 'text'),
            ),
            returns=('bool',),
            text="""
                SELECT COALESCE(
                    has_schema_privilege(schema_oid, privilege), TRUE
                )
            """
        ),
        dbops.Function(
            name=('edgedbsql', 'has_table_privilege'),
            args=(
                ('table_name', 'text'),
                ('privilege', 'text'),
            ),
            returns=('bool',),
            text="""
                SELECT has_table_privilege(oid, privilege)
                FROM edgedbsql.pg_class
                WHERE relname = table_name;
            """
        ),
        dbops.Function(
            name=('edgedbsql', 'has_table_privilege'),
            args=(
                ('schema_oid', 'oid'),
                ('privilege', 'text'),
            ),
            returns=('bool',),
            text="""
                SELECT has_table_privilege(schema_oid, privilege)
            """
        ),

        dbops.Function(
            name=('edgedbsql', 'has_column_privilege'),
            args=(
                ('tbl', 'oid'),
                ('col', 'smallint'),
                ('privilege', 'text'),
            ),
            returns=('bool',),
            text="""
                SELECT has_column_privilege(tbl, col, privilege)
            """
        ),
        dbops.Function(
            name=('edgedbsql', 'has_column_privilege'),
            args=(
                ('tbl', 'text'),
                ('col', 'smallint'),
                ('privilege', 'text'),
            ),
            returns=('bool',),
            text="""
                SELECT has_column_privilege(oid, col, privilege)
                FROM edgedbsql.pg_class
                WHERE relname = tbl;
            """
        ),
        dbops.Function(
            name=('edgedbsql', 'has_column_privilege'),
            args=(
                ('tbl', 'oid'),
                ('col', 'text'),
                ('privilege', 'text'),
            ),
            returns=('bool',),
            text="""
                SELECT has_column_privilege(tbl, attnum, privilege)
                FROM edgedbsql.pg_attribute pa
                WHERE attrelid = tbl AND attname = col
            """
        ),
        dbops.Function(
            name=('edgedbsql', 'has_column_privilege'),
            args=(
                ('tbl', 'text'),
                ('col', 'text'),
                ('privilege', 'text'),
            ),
            returns=('bool',),
            text="""
                SELECT has_column_privilege(pc.oid, attnum, privilege)
                FROM edgedbsql.pg_class pc
                JOIN edgedbsql.pg_attribute pa ON pa.attrelid = pc.oid
                WHERE pc.relname = tbl AND pa.attname = col;
            """
        ),
        dbops.Function(
            name=('edgedbsql', '_pg_truetypid'),
            args=(
                ('att', ('edgedbsql', 'pg_attribute')),
                ('typ', ('edgedbsql', 'pg_type')),
            ),
            returns=('oid',),
            volatility='IMMUTABLE',
            strict=True,
            text="""
                SELECT CASE
                    WHEN typ.typtype = 'd' THEN typ.typbasetype
                    ELSE att.atttypid
                END
            """
        ),
        dbops.Function(
            name=('edgedbsql', '_pg_truetypmod'),
            args=(
                ('att', ('edgedbsql', 'pg_attribute')),
                ('typ', ('edgedbsql', 'pg_type')),
            ),
            returns=('int4',),
            volatility='IMMUTABLE',
            strict=True,
            text="""
                SELECT CASE
                    WHEN typ.typtype = 'd' THEN typ.typtypmod
                    ELSE att.atttypmod
                END
            """
        ),
        dbops.Function(
            name=('edgedbsql', 'pg_table_is_visible'),
            args=[
                ('id', ('oid',)),
                ('search_path', ('text[]',)),
            ],
            returns=('bool',),
            volatility='stable',
            text=r'''
                SELECT pc.relnamespace IN (
                    SELECT oid
                    FROM edgedbsql.pg_namespace pn
                    WHERE pn.nspname IN (select * from unnest(search_path))
                )
                FROM edgedbsql.pg_class pc
                WHERE id = pc.oid
            '''
        )
    ]

    return (
        [cast(dbops.Command, dbops.CreateFunction(uuid_to_oid))]
        + [dbops.CreateView(virtual_tables)]
        + [
            cast(dbops.Command, dbops.CreateFunction(long_name)),
            cast(dbops.Command, dbops.CreateFunction(type_rename)),
            cast(dbops.Command, dbops.CreateFunction(namespace_rename)),
        ]
        + [dbops.CreateView(view) for view in views]
        + [dbops.CreateFunction(func) for func in util_functions]
    )


def get_config_type_views(
    schema: s_schema.Schema,
    conf: s_objtypes.ObjectType,
    scope: Optional[qltypes.ConfigScope],
) -> dbops.CommandGroup:
    commands = dbops.CommandGroup()

    cfg_views, _ = _generate_config_type_view(
        schema,
        conf,
        scope=scope,
        path=[],
        rptr=None,
    )
    commands.add_commands([
        dbops.CreateView(dbops.View(name=tn, query=q), or_replace=True)
        for tn, q in cfg_views
    ])

    return commands


def get_config_views(
    schema: s_schema.Schema,
) -> dbops.CommandGroup:
    commands = dbops.CommandGroup()

    conf = schema.get('cfg::Config', type=s_objtypes.ObjectType)
    commands.add_command(
        get_config_type_views(schema, conf, scope=None),
    )

    conf = schema.get('cfg::InstanceConfig', type=s_objtypes.ObjectType)
    commands.add_command(
        get_config_type_views(schema, conf, scope=qltypes.ConfigScope.INSTANCE),
    )

    conf = schema.get('cfg::DatabaseConfig', type=s_objtypes.ObjectType)
    commands.add_command(
        get_config_type_views(schema, conf, scope=qltypes.ConfigScope.DATABASE),
    )

    return commands


def get_support_views(
    schema: s_schema.Schema,
    backend_params: params.BackendRuntimeParams,
) -> dbops.CommandGroup:
    commands = dbops.CommandGroup()

    schema_alias_views = _generate_schema_alias_views(
        schema, s_name.UnqualName('schema'))

    InhObject = schema.get(
        'schema::InheritingObject', type=s_objtypes.ObjectType)
    InhObject__ancestors = InhObject.getptr(
        schema, s_name.UnqualName('ancestors'), type=s_links.Link)
    schema_alias_views.append(
        _generate_schema_alias_view(schema, InhObject__ancestors))

    ObjectType = schema.get(
        'schema::ObjectType', type=s_objtypes.ObjectType)
    ObjectType__ancestors = ObjectType.getptr(
        schema, s_name.UnqualName('ancestors'), type=s_links.Link)
    schema_alias_views.append(
        _generate_schema_alias_view(schema, ObjectType__ancestors))

    for alias_view in schema_alias_views:
        commands.add_command(dbops.CreateView(alias_view, or_replace=True))

    commands.add_command(get_config_views(schema))

    for dbview in _generate_database_views(schema):
        commands.add_command(dbops.CreateView(dbview, or_replace=True))

    for extview in _generate_extension_views(schema):
        commands.add_command(dbops.CreateView(extview, or_replace=True))

    if backend_params.has_create_role:
        role_views = _generate_role_views(schema)
    else:
        role_views = _generate_single_role_views(schema)
    for roleview in role_views:
        commands.add_command(dbops.CreateView(roleview, or_replace=True))

    for verview in _generate_schema_ver_views(schema):
        commands.add_command(dbops.CreateView(verview, or_replace=True))

    sys_alias_views = _generate_schema_alias_views(
        schema, s_name.UnqualName('sys'))
    for alias_view in sys_alias_views:
        commands.add_command(dbops.CreateView(alias_view, or_replace=True))

    commands.add_commands(_generate_sql_information_schema())

    return commands


async def generate_support_views(
    conn: PGConnection,
    schema: s_schema.Schema,
    backend_params: params.BackendRuntimeParams,
) -> None:
    commands = get_support_views(schema, backend_params)
    block = dbops.PLTopBlock()
    commands.generate(block)
    await _execute_block(conn, block)


async def generate_support_functions(
    conn: PGConnection,
    schema: s_schema.Schema,
) -> None:
    commands = dbops.CommandGroup()

    commands.add_commands([
        dbops.CreateFunction(IssubclassFunction()),
        dbops.CreateFunction(IssubclassFunction2()),
        dbops.CreateFunction(GetSchemaObjectNameFunction()),
        dbops.CreateFunction(FormatTypeFunction()),
    ])

    block = dbops.PLTopBlock()
    commands.generate(block)
    await _execute_block(conn, block)


async def generate_more_support_functions(
    conn: PGConnection,
    compiler: edbcompiler.Compiler,
    schema: s_schema.Schema,
    testmode: bool,
) -> None:
    commands = dbops.CommandGroup()

    commands.add_commands([
        dbops.CreateFunction(
            DescribeRolesAsDDLFunction(schema), or_replace=True),
        dbops.CreateFunction(GetSequenceBackendNameFunction()),
        dbops.CreateFunction(DumpSequencesFunction()),
    ])

    block = dbops.PLTopBlock()
    commands.generate(block)
    await _execute_block(conn, block)


def _build_key_source(
    schema: s_schema.Schema,
    exc_props: Iterable[s_pointers.Pointer],
    rptr: Optional[s_pointers.Pointer],
    source_idx: str,
) -> str:
    if exc_props:
        restargets = []
        for prop in exc_props:
            pname = prop.get_shortname(schema).name
            restarget = f'(q{source_idx}.val)->>{ql(pname)}'
            restargets.append(restarget)

        targetlist = ','.join(restargets)

        keysource = f'''
            (SELECT
                ARRAY[{targetlist}] AS key
            ) AS k{source_idx}'''
    else:
        assert rptr is not None
        rptr_name = rptr.get_shortname(schema).name
        keysource = f'''
            (SELECT
                ARRAY[
                    (CASE WHEN q{source_idx}.val = 'null'::jsonb
                     THEN NULL
                     ELSE {ql(rptr_name)}
                     END)
                ] AS key
            ) AS k{source_idx}'''

    return keysource


def _build_key_expr(key_components: List[str]) -> str:
    key_expr = ' || '.join(key_components)
    final_keysource = f'''
        (SELECT
            (CASE WHEN array_position(q.v, NULL) IS NULL
             THEN
                 edgedb.uuid_generate_v5(
                     '{DATABASE_ID_NAMESPACE}'::uuid,
                     array_to_string(q.v, ';')
                 )
             ELSE NULL
             END) AS key
         FROM
            (SELECT {key_expr} AS v) AS q
        )'''

    return final_keysource


def _build_data_source(
    schema: s_schema.Schema,
    rptr: s_pointers.Pointer,
    source_idx: int,
    *,
    always_array: bool = False,
    alias: Optional[str] = None,
) -> str:

    rptr_name = rptr.get_shortname(schema).name
    rptr_card = rptr.get_cardinality(schema)
    rptr_multi = rptr_card.is_multi()

    if alias is None:
        alias = f'q{source_idx + 1}'
    else:
        alias = f'q{alias}'

    if rptr_multi:
        sourceN = f'''
            (SELECT jel.val
                FROM
                jsonb_array_elements(
                    (q{source_idx}.val)->{ql(rptr_name)}) AS jel(val)
            ) AS {alias}'''
    else:
        proj = '[0]' if always_array else ''
        sourceN = f'''
            (SELECT
                (q{source_idx}.val){proj}->{ql(rptr_name)} AS val
            ) AS {alias}'''

    return sourceN


def _escape_like(s: str) -> str:
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')


def _generate_config_type_view(
    schema: s_schema.Schema,
    stype: s_objtypes.ObjectType,
    *,
    scope: Optional[qltypes.ConfigScope],
    path: List[Tuple[s_pointers.Pointer, List[s_pointers.Pointer]]],
    rptr: Optional[s_pointers.Pointer],
    _memo: Optional[Set[s_obj.Object]] = None,
) -> Tuple[
    List[Tuple[Tuple[str, str], str]],
    List[s_pointers.Pointer],
]:
    X = xdedent.escape

    exc = schema.get('std::exclusive', type=s_constr.Constraint)

    if scope is not None:
        if scope is qltypes.ConfigScope.INSTANCE:
            max_source = "'system override'"
        elif scope is qltypes.ConfigScope.DATABASE:
            max_source = "'database'"
        else:
            raise AssertionError(f'unexpected config scope: {scope!r}')
    else:
        max_source = 'NULL'

    if _memo is None:
        _memo = set()

    _memo.add(stype)

    views = []

    sources = []

    ext_cfg = schema.get('cfg::ExtensionConfig', type=s_objtypes.ObjectType)
    is_ext_cfg = stype.issubclass(schema, ext_cfg)
    if is_ext_cfg:
        rptr = None
    is_rptr_ext_cfg = False

    if not path:
        if is_ext_cfg:
            # Extension configs get one object per scope.
            cfg_name = str(stype.get_name(schema))

            escaped_name = _escape_like(cfg_name)
            source0 = f'''
                (SELECT
                    (SELECT jsonb_object_agg(
                      substr(name, {len(cfg_name)+3}), value) AS val
                    FROM edgedb._read_sys_config(
                      NULL, scope::edgedb._sys_config_source_t) cfg
                    WHERE name LIKE {ql(escaped_name + '%')}
                    ) AS val, scope::text AS scope, scope_id AS scope_id
                    FROM (VALUES
                        (NULL, '{CONFIG_ID[None]}'::uuid),
                        ('database',
                         '{CONFIG_ID[qltypes.ConfigScope.DATABASE]}'::uuid)
                    ) AS s(scope, scope_id)
                ) AS q0
            '''
        elif rptr is None:
            # This is the root config object.
            source0 = f'''
                (SELECT jsonb_object_agg(name, value) AS val
                FROM edgedb._read_sys_config(NULL, {max_source}) cfg) AS q0'''
        else:
            rptr_name = rptr.get_shortname(schema).name
            rptr_source = not_none(rptr.get_source(schema))
            is_rptr_ext_cfg = rptr_source.issubclass(schema, ext_cfg)
            if is_rptr_ext_cfg:
                cfg_name = str(rptr_source.get_name(schema)) + '::' + rptr_name
                escaped_name = _escape_like(cfg_name)

                source0 = f'''
                    (SELECT el.val AS val, s.scope::text AS scope,
                            s.scope_id AS scope_id
                     FROM (VALUES
                         (NULL, '{CONFIG_ID[None]}'::uuid),
                         ('database',
                          '{CONFIG_ID[qltypes.ConfigScope.DATABASE]}'::uuid)
                     ) AS s(scope, scope_id),
                     LATERAL (
                         SELECT (value::jsonb) AS val
                         FROM edgedb._read_sys_config(
                           NULL, scope::edgedb._sys_config_source_t) cfg
                         WHERE name LIKE {ql(escaped_name + '%')}
                     ) AS cfg,
                     LATERAL jsonb_array_elements(cfg.val) AS el(val)
                    ) AS q0
                '''

            else:
                source0 = f'''
                    (SELECT el.val
                     FROM
                        (SELECT (value::jsonb) AS val
                        FROM edgedb._read_sys_config(NULL, {max_source})
                        WHERE name = {ql(rptr_name)}) AS cfg,
                        LATERAL jsonb_array_elements(cfg.val) AS el(val)
                    ) AS q0'''

        sources.append(source0)
        key_start = 0
    else:
        # XXX: The second level is broken for extension configs.
        # Can we solve this without code duplication?
        root = path[0][0]
        root_source = not_none(root.get_source(schema))
        is_root_ext_cfg = root_source.issubclass(schema, ext_cfg)
        assert not is_root_ext_cfg, (
            "nested conf objects not yet supported for ext configs")

        key_start = 0

        for i, (l, exc_props) in enumerate(path):
            l_card = l.get_cardinality(schema)
            l_multi = l_card.is_multi()
            l_name = l.get_shortname(schema).name

            if i == 0:
                if l_multi:
                    sourceN = f'''
                        (SELECT el.val
                        FROM
                            (SELECT (value::jsonb) AS val
                            FROM edgedb._read_sys_config(NULL, {max_source})
                            WHERE name = {ql(l_name)}) AS cfg,
                            LATERAL jsonb_array_elements(cfg.val) AS el(val)
                        ) AS q{i}'''
                else:
                    sourceN = f'''
                        (SELECT (value::jsonb) AS val
                        FROM edgedb._read_sys_config(NULL, {max_source}) cfg
                        WHERE name = {ql(l_name)}) AS q{i}'''
            else:
                sourceN = _build_data_source(schema, l, i - 1)

            sources.append(sourceN)
            sources.append(_build_key_source(schema, exc_props, l, str(i)))

            if exc_props:
                key_start = i

    exclusive_props = []
    single_links = []
    multi_links = []
    multi_props = []
    target_cols: dict[s_pointers.Pointer, str] = {}
    where = ''

    path_steps = [p.get_shortname(schema).name for p, _ in path]

    if rptr is not None:
        self_idx = len(path)

        # Generate a source rvar for _this_ target
        rptr_name = rptr.get_shortname(schema).name
        path_steps.append(rptr_name)

        if self_idx > 0:
            sourceN = _build_data_source(schema, rptr, self_idx - 1)
            sources.append(sourceN)
    else:
        self_idx = 0

    sval = f'(q{self_idx}.val)'

    for pp_name, pp in stype.get_pointers(schema).items(schema):
        pn = str(pp_name)
        if pn in ('id', '__type__'):
            continue

        pp_type = pp.get_target(schema)
        assert pp_type is not None
        pp_card = pp.get_cardinality(schema)
        pp_multi = pp_card.is_multi()
        pp_psi = types.get_pointer_storage_info(pp, schema=schema)
        pp_col = pp_psi.column_name

        if isinstance(pp, s_links.Link):
            if pp_multi:
                multi_links.append(pp)
            else:
                single_links.append(pp)
        else:
            pp_cast = _make_json_caster(schema, pp_type)

            if pp_multi:
                multi_props.append((pp, pp_cast))
            else:
                extract_col = (
                    f'{pp_cast(f"{sval}->{ql(pn)}")} AS {qi(pp_col)}')

                target_cols[pp] = extract_col

                constraints = pp.get_constraints(schema).objects(schema)
                if any(c.issubclass(schema, exc) for c in constraints):
                    exclusive_props.append(pp)

    exclusive_props.sort(key=lambda p: p.get_shortname(schema).name)

    if is_ext_cfg:
        # Extension configs get custom keys based on their type name
        # and the scope, since we create one object per scope.
        key_components = [
            f'ARRAY[{ql(str(stype.get_name(schema)))}]',
            "ARRAY[coalesce(q0.scope, 'session')]"
        ]
        final_keysource = f'{_build_key_expr(key_components)} AS k'
        sources.append(final_keysource)

        key_expr = 'k.key'
        where = f"q0.val IS NOT NULL"

    elif exclusive_props or rptr:
        sources.append(
            _build_key_source(schema, exclusive_props, rptr, str(self_idx)))

        key_components = [f'k{i}.key' for i in range(key_start, self_idx + 1)]
        if is_rptr_ext_cfg:
            assert rptr_source
            key_components = [
                f'ARRAY[{ql(str(rptr_source.get_name(schema)))}]',
                "ARRAY[coalesce(q0.scope, 'session')]"
            ] + key_components

        final_keysource = f'{_build_key_expr(key_components)} AS k'
        sources.append(final_keysource)

        key_expr = 'k.key'

        tname = str(stype.get_name(schema))
        where = f"{key_expr} IS NOT NULL AND ({sval}->>'_tname') = {ql(tname)}"

    else:
        key_expr = f"'{CONFIG_ID[scope]}'::uuid"

        key_components = []

    id_ptr = stype.getptr(schema, s_name.UnqualName('id'))
    target_cols[id_ptr] = f'{X(key_expr)} AS id'

    base_sources = list(sources)

    for link in single_links:
        link_name = link.get_shortname(schema).name
        link_type = link.get_target(schema)
        link_psi = types.get_pointer_storage_info(link, schema=schema)
        link_col = link_psi.column_name

        if str(link_type.get_name(schema)) == 'cfg::AbstractConfig':
            target_cols[link] = f'q0.scope_id AS {qi(link_col)}'
            continue

        if rptr is not None:
            target_path = path + [(rptr, exclusive_props)]
        else:
            target_path = path

        target_views, target_exc_props = _generate_config_type_view(
            schema,
            link_type,
            scope=scope,
            path=target_path,
            rptr=link,
            _memo=_memo,
        )

        for descendant in link_type.descendants(schema):
            if descendant not in _memo:
                desc_views, _ = _generate_config_type_view(
                    schema,
                    descendant,
                    scope=scope,
                    path=target_path,
                    rptr=link,
                    _memo=_memo,
                )
                views.extend(desc_views)

        target_source = _build_data_source(
            schema, link, self_idx, alias=link_name,
            always_array=rptr is None,
        )
        sources.append(target_source)

        target_key_source = _build_key_source(
            schema, target_exc_props, link, source_idx=link_name)
        sources.append(target_key_source)

        target_key_components = key_components + [f'k{link_name}.key']

        target_key = _build_key_expr(target_key_components)
        target_cols[link] = f'({X(target_key)}) AS {qi(link_col)}'

        views.extend(target_views)

    # You can't change the order of a postgres view... so
    # sort by the order the pointers were added to the source.
    # N.B: This only works because we are using the original in-memory
    # schema. If it was loaded from reflection it probably wouldn't
    # work.
    ptr_indexes = {
        v: i for i, v in enumerate(stype.get_pointers(schema).objects(schema))
    }
    target_cols_sorted = sorted(
        target_cols.items(), key=lambda p: ptr_indexes[p[0]]
    )

    target_cols_str = ',\n'.join([x for _, x in target_cols_sorted if x])

    fromlist = ',\n'.join(f'LATERAL {X(s)}' for s in sources)

    target_query = xdedent.xdedent(f'''
        SELECT
            {X(target_cols_str)}
        FROM
            {X(fromlist)}
    ''')

    if where:
        target_query += f'\nWHERE\n    {where}'

    views.append((tabname(schema, stype), target_query))

    for link in multi_links:
        target_sources = list(base_sources)

        link_name = link.get_shortname(schema).name
        link_type = link.get_target(schema)

        if rptr is not None:
            target_path = path + [(rptr, exclusive_props)]
        else:
            target_path = path

        target_views, target_exc_props = _generate_config_type_view(
            schema,
            link_type,
            scope=scope,
            path=target_path,
            rptr=link,
            _memo=_memo,
        )
        views.extend(target_views)

        for descendant in link_type.descendants(schema):
            if descendant not in _memo:
                desc_views, _ = _generate_config_type_view(
                    schema,
                    descendant,
                    scope=scope,
                    path=target_path,
                    rptr=link,
                    _memo=_memo,
                )
                views.extend(desc_views)

        # HACK: For computable links (just extensions hopefully?), we
        # want to compile the targets as a side effect, but we don't
        # want to actually include them in the view.
        if link.get_computable(schema):
            continue

        target_source = _build_data_source(
            schema, link, self_idx, alias=link_name)
        target_sources.append(target_source)

        target_key_source = _build_key_source(
            schema, target_exc_props, link, source_idx=link_name)
        target_sources.append(target_key_source)

        target_key_components = key_components + [f'k{link_name}.key']
        target_key = _build_key_expr(target_key_components)

        target_fromlist = ',\n'.join(f'LATERAL {X(s)}' for s in target_sources)

        link_query = xdedent.xdedent(f'''\
            SELECT
                q.source,
                q.target
            FROM
                (SELECT
                    {X(key_expr)} AS source,
                    {X(target_key)} AS target
                FROM
                    {X(target_fromlist)}
                ) q
            WHERE
                q.target IS NOT NULL
            ''')

        views.append((tabname(schema, link), link_query))

    for prop, pp_cast in multi_props:
        target_sources = list(sources)

        pn = prop.get_shortname(schema).name

        target_source = _build_data_source(
            schema, prop, self_idx, alias=pn)
        target_sources.append(target_source)

        target_fromlist = ',\n'.join(f'LATERAL {X(s)}' for s in target_sources)

        link_query = xdedent.xdedent(f'''\
            SELECT
                {X(key_expr)} AS source,
                {pp_cast(f'q{pn}.val')} AS target
            FROM
                {X(target_fromlist)}
        ''')

        views.append((tabname(schema, prop), link_query))

    return views, exclusive_props


async def _execute_block(
    conn: PGConnection,
    block: dbops.SQLBlock,
) -> None:
    await execute_sql_script(conn, block.to_string())


async def execute_sql_script(
    conn: PGConnection,
    sql_text: str,
) -> None:
    from edb.server import pgcon

    if debug.flags.bootstrap:
        debug.header('Bootstrap Script')
        if len(sql_text) > 102400:
            # Make sure we don't hog CPU by attempting to highlight
            # huge scripts.
            print(sql_text)
        else:
            debug.dump_code(sql_text, lexer='sql')

    try:
        await conn.sql_execute(sql_text.encode("utf-8"))
    except pgcon.BackendError as e:
        position = e.get_field('P')
        internal_position = e.get_field('p')
        context = e.get_field('W')
        if context:
            pl_func_line_m = re.search(
                r'^PL/pgSQL function inline_code_block line (\d+).*',
                context, re.M)

            if pl_func_line_m:
                pl_func_line = int(pl_func_line_m.group(1))
        else:
            pl_func_line = None

        point = None
        text = None

        if position is not None:
            point = int(position)
            text = sql_text

        elif internal_position is not None:
            point = int(internal_position)
            text = e.get_field('q')

        elif pl_func_line:
            point = ql_parser.offset_of_line(sql_text, pl_func_line)
            text = sql_text

        if point is not None:
            pcontext = parser_context.ParserContext(
                'query', text, start=point, end=point, context_lines=30)
            exceptions.replace_context(e, pcontext)

        raise
