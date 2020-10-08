#
# This source file is part of the EdgeDB open source project.
#
# Copyright 2008-present MagicStack Inc. and the EdgeDB authors.
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


from __future__ import annotations

import textwrap

from ..common import qname as qn
from ..common import quote_ident as qi
from ..common import quote_literal as ql

from . import base
from . import ddl


class EnumExists(base.Condition):
    def __init__(self, name):
        self.name = name

    def code(self, block: base.PLBlock) -> str:
        return textwrap.dedent(f'''\
            SELECT
                t.typname
            FROM
                pg_catalog.pg_type t
                INNER JOIN pg_namespace nsp
                    ON (t.typnamespace = nsp.oid)
            WHERE
                nsp.nspname = {ql(self.name[0])}
                AND t.typname = {ql(self.name[1])}
                AND t.typtype = 'e'
        ''')


class Enum(base.DBObject):

    def __init__(self, name, values, *, metadata=None):
        self.name = name
        self.values = values
        super().__init__(metadata=metadata)


class CreateEnum(ddl.SchemaObjectOperation):
    def __init__(
            self, enum, *, conditions=None, neg_conditions=None,
            priority=0):
        super().__init__(
            enum.name, conditions=conditions, neg_conditions=neg_conditions,
            priority=priority)
        self.values = enum.values

    def code(self, block: base.PLBlock) -> str:
        vals = ', '.join(ql(v) for v in self.values)
        return f'CREATE TYPE {qn(*self.name)} AS ENUM ({vals})'


class RenameEnum(base.CommandGroup):
    def __init__(
            self, name, new_name, *, conditions=None, neg_conditions=None,
            priority=0):
        super().__init__(
            conditions=conditions, neg_conditions=neg_conditions,
            priority=priority)

        if name[0] != new_name[0]:
            cmd = AlterEnumSetSchema(name, new_name[0])
            self.add_command(cmd)
            name = (new_name[0], name[1])

        if name[1] != new_name[1]:
            cmd = AlterEnumRenameTo(name, new_name[1])
            self.add_command(cmd)


class AlterEnumSetSchema(ddl.DDLOperation):
    def __init__(
            self, name, new_schema, *, conditions=None, neg_conditions=None,
            priority=0):
        super().__init__(
            conditions=conditions, neg_conditions=neg_conditions,
            priority=priority)
        self.name = name
        self.new_schema = new_schema

    def code(self, block: base.PLBlock) -> str:
        return (f'ALTER TYPE {qn(*self.name)} '
                f'SET SCHEMA {qi(self.new_schema)}')


class AlterEnumRenameTo(ddl.DDLOperation):
    def __init__(
            self, name, new_name, *, conditions=None, neg_conditions=None,
            priority=0):
        super().__init__(
            conditions=conditions, neg_conditions=neg_conditions,
            priority=priority)
        self.name = name
        self.new_name = new_name

    def code(self, block: base.PLBlock) -> str:
        return (f'ALTER TYPE {qn(*self.name)} '
                f'RENAME TO {qi(self.new_name)}')


class AlterEnum(ddl.DDLOperation):
    def __init__(
            self, name, *, conditions=None, neg_conditions=None, priority=0):
        super().__init__(
            conditions=conditions, neg_conditions=neg_conditions,
            priority=priority)
        self.name = name

    def prefix_code(self) -> str:
        return f'ALTER TYPE {qn(*self.name)}'

    def __repr__(self):
        return '<edb.sync.%s %s>' % (self.__class__.__name__, self.name)


class AlterEnumAddValue(AlterEnum):
    def __init__(self, name, value, *, before=None, after=None,
                 conditional=False):
        super().__init__(name)
        self.value = value
        self.before = before
        self.after = after
        self.conditional = conditional

    def code(self, block: base.PLBlock) -> str:
        code = self.prefix_code()
        code += ' ADD VALUE'
        if self.conditional:
            code += ' IF NOT EXISTS'
        code += f' {ql(self.value)}'
        if self.before:
            code += f' BEFORE {ql(self.before)}'
        elif self.after:
            code += f' AFTER {ql(self.before)}'

        return code


class AlterEnumRenameValue(AlterEnum):
    def __init__(self, name, old_value, new_value):
        super().__init__(name)
        self.old_value = old_value
        self.new_value = new_value

    def code(self, block: base.PLBlock) -> str:
        code = self.prefix_code()
        code += f' RENAME VALUE {ql(self.old_value)} TO {ql(self.new_value)}'
        return code


class DropEnum(ddl.SchemaObjectOperation):
    def code(self, block: base.PLBlock) -> str:
        return f'DROP TYPE {qn(*self.name)}'
