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


import collections

from edb.lang.common import ordered, struct
from edb.lang.edgeql import ast as qlast

from . import delta as sd
from . import error as s_err
from . import inheriting
from . import objects as so
from . import name as sn
from . import named
from . import utils


class RefDict(struct.Struct):

    local_attr = struct.Field(str)
    attr = struct.Field(str)
    backref_attr = struct.Field(str, default='subject')
    requires_explicit_inherit = struct.Field(bool, default=False)
    ref_cls = struct.Field(type)


class RebaseReferencingObject(inheriting.RebaseNamedObject):
    def apply(self, schema, context):
        schema, this_obj = super().apply(schema, context)

        objects = [this_obj] + list(this_obj.descendants(schema))
        for obj in objects:
            for refdict in this_obj.__class__.get_refdicts():
                attr = refdict.attr
                local_attr = refdict.local_attr
                backref = refdict.backref_attr

                coll: dict = getattr(obj, attr)
                local_coll: dict = getattr(obj, local_attr)

                for ref_name in tuple(coll.names(schema)):
                    if not local_coll.has(schema, ref_name):
                        try:
                            obj.get_classref_origin(
                                schema, ref_name, attr, local_attr, backref)
                        except KeyError:
                            del coll[ref_name]

        return schema, this_obj


class ReferencingObjectMeta(type(inheriting.InheritingObject)):
    def __new__(mcls, name, bases, clsdict):
        refdicts = collections.OrderedDict()
        mydicts = {k: v for k, v in clsdict.items() if isinstance(v, RefDict)}
        cls = super().__new__(mcls, name, bases, clsdict)

        for parent in reversed(cls.__mro__):
            if parent is cls:
                refdicts.update(mydicts)
            elif isinstance(parent, ReferencingObjectMeta):
                refdicts.update({k: d.copy()
                                for k, d in parent.get_own_refdicts().items()})

        cls._refdicts_by_refclass = {}

        for dct in refdicts.values():
            if dct.attr not in cls._fields:
                raise RuntimeError(
                    f'object {name} has no refdict field {dct.attr}')
            if dct.local_attr not in cls._fields:
                raise RuntimeError(
                    f'object {name} has no refdict field {dct.local_attr}')

            if cls._fields[dct.attr].inheritable:
                raise RuntimeError(
                    f'{name}.{dct.attr} field must not be inheritable')
            if cls._fields[dct.local_attr].inheritable:
                raise RuntimeError(
                    f'{name}.{dct.local_attr} field must not be inheritable')
            if not cls._fields[dct.attr].ephemeral:
                raise RuntimeError(
                    f'{name}.{dct.attr} field must be ephemeral')
            if not cls._fields[dct.local_attr].ephemeral:
                raise RuntimeError(
                    f'{name}.{dct.local_attr} field must be ephemeral')
            if not cls._fields[dct.attr].coerce:
                raise RuntimeError(
                    f'{name}.{dct.attr} field must be coerced')
            if not cls._fields[dct.local_attr].coerce:
                raise RuntimeError(
                    f'{name}.{dct.local_attr} field must be coerced')

            if isinstance(dct.ref_cls, str):
                ref_cls_getter = getattr(cls, dct.ref_cls)
                try:
                    dct.ref_cls = ref_cls_getter()
                except NotImplementedError:
                    pass

            if not isinstance(dct.ref_cls, str):
                other_dct = cls._refdicts_by_refclass.get(dct.ref_cls)
                if other_dct is not None:
                    raise TypeError(
                        'multiple reference dicts for {!r} in '
                        '{!r}: {!r} and {!r}'.format(dct.ref_cls, cls,
                                                     dct.attr, other_dct.attr))

                cls._refdicts_by_refclass[dct.ref_cls] = dct

        # Refdicts need to be reversed here to respect the __mro__,
        # as we have iterated over it in reverse above.
        cls._refdicts = collections.OrderedDict(reversed(refdicts.items()))

        cls._refdicts_by_field = {rd.attr: rd for rd in cls._refdicts.values()}

        setattr(cls, '{}.{}_refdicts'.format(cls.__module__, cls.__name__),
                     mydicts)

        return cls

    def get_own_refdicts(cls):
        return getattr(cls, '{}.{}_refdicts'.format(
            cls.__module__, cls.__name__))

    def get_refdicts(cls):
        return iter(cls._refdicts.values())

    def get_refdict(cls, name):
        return cls._refdicts_by_field.get(name)

    def get_refdict_for_class(cls, refcls):
        for rcls in refcls.__mro__:
            try:
                return cls._refdicts_by_refclass[rcls]
            except KeyError:
                pass
        else:
            raise KeyError(f'{cls} has no refdict for {refcls}')


class ReferencedObjectCommandMeta(type(named.NamedObjectCommand)):
    _transparent_adapter_subclass = True

    def __new__(mcls, name, bases, clsdct, *,
                referrer_context_class=None, **kwargs):
        cls = super().__new__(mcls, name, bases, clsdct, **kwargs)
        if referrer_context_class is not None:
            cls._referrer_context_class = referrer_context_class
        return cls


class ReferencedObjectCommand(named.NamedObjectCommand,
                              metaclass=ReferencedObjectCommandMeta):
    _referrer_context_class = None

    @classmethod
    def get_referrer_context_class(cls):
        if cls._referrer_context_class is None:
            raise TypeError(
                f'referrer_context_class is not defined for {cls}')
        return cls._referrer_context_class

    @classmethod
    def get_referrer_context(cls, context):
        return context.get(cls.get_referrer_context_class())

    @classmethod
    def _classname_from_ast(cls, schema, astnode, context):
        name = super()._classname_from_ast(schema, astnode, context)

        parent_ctx = cls.get_referrer_context(context)
        if parent_ctx is not None:
            referrer_name = parent_ctx.op.classname

            try:
                base_ref = utils.ast_to_typeref(
                    qlast.TypeName(maintype=astnode.name),
                    modaliases=context.modaliases, schema=schema)
            except s_err.ItemNotFoundError:
                base_name = sn.Name(name)
            else:
                base_name = base_ref.classname

            pcls = cls.get_schema_metaclass()
            pnn = pcls.get_specialized_name(base_name, referrer_name)

            name = sn.Name(name=pnn, module=referrer_name.module)

        return name

    def _get_ast_node(self, context):
        subject_ctx = self.get_referrer_context(context)
        ref_astnode = getattr(self, 'referenced_astnode', None)
        if subject_ctx is not None and ref_astnode is not None:
            return ref_astnode
        else:
            if isinstance(self.astnode, (list, tuple)):
                return self.astnode[1]
            else:
                return self.astnode

    def _create_innards(self, schema, context):
        schema = super()._create_innards(schema, context)

        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is not None:
            referrer = referrer_ctx.scls
            refdict = referrer.__class__.get_refdict_for_class(
                self.scls.__class__)

            if refdict.backref_attr:
                # Set the back-reference on referenced object
                # to the referrer.
                setattr(self.scls, refdict.backref_attr, referrer)
                # Add the newly created referenced object to the
                # appropriate refdict in self and all descendants
                # that don't already have an existing reference.
                #
                schema = referrer.add_classref(
                    schema, refdict.attr, self.scls)
                refname = self.scls.get_shortname(self.scls.name)
                for child in referrer.descendants(schema):
                    child_local_coll = getattr(child, refdict.local_attr)
                    child_coll = getattr(child, refdict.attr)
                    if not child_local_coll.has(schema, refname):
                        schema, child_coll = child_coll.replace(
                            schema, {refname: self.scls})
                        setattr(child, refdict.attr, child_coll)

        return schema

    def _rename_innards(self, schema, context, scls):
        schema = super()._rename_innards(schema, context, scls)

        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is not None:
            referrer = referrer_ctx.scls
            old_name = scls.get_shortname(self.old_name)
            new_name = scls.get_shortname(self.new_name)

            if old_name == new_name:
                return schema

            refdict = referrer.__class__.get_refdict_for_class(
                scls.__class__)

            attr = refdict.attr
            local_attr = refdict.local_attr

            coll = getattr(referrer, attr)
            if coll.has(schema, old_name):
                ref = coll[old_name]
                schema, coll = coll.replace(
                    schema, {old_name: None, new_name: ref})
                setattr(referrer, attr, coll)

            local_coll = getattr(referrer, local_attr)
            if local_coll.has(schema, old_name):
                local = local_coll[old_name]
                schema, local_coll = local_coll.replace(
                    schema, {old_name: None, new_name: local})
                setattr(referrer, local_attr, local_coll)

            for child in referrer.children(schema):
                child_coll = getattr(child, attr)
                if child_coll.has(schema, old_name):
                    ref = child_coll[old_name]
                    schema, child_coll = child_coll.replace(
                        schema, {old_name: None, new_name: ref})
                    setattr(child, attr, child_coll)

        return schema

    def _delete_innards(self, schema, context, scls):
        schema = super()._delete_innards(schema, context, scls)

        referrer_ctx = self.get_referrer_context(context)
        if referrer_ctx is not None:
            referrer = referrer_ctx.scls
            refdict = referrer.__class__.get_refdict_for_class(
                scls.__class__)
            schema = referrer.del_classref(
                schema, refdict.attr, scls.name)

        return schema


class ReferencedInheritingObjectCommand(
        ReferencedObjectCommand, inheriting.InheritingObjectCommand):

    def _create_begin(self, schema, context):
        referrer_ctx = self.get_referrer_context(context)
        attrs = self.get_struct_properties(schema)

        if referrer_ctx is not None and not attrs.get('is_derived'):
            mcls = self.get_schema_metaclass()
            referrer = referrer_ctx.scls
            basename = mcls.get_shortname(self.classname)
            base = schema.get(basename, type=mcls)
            schema, self.scls = base.derive(schema, referrer, attrs=attrs,
                                            add_to_schema=True,
                                            init_props=False)
            return schema
        else:
            return super()._create_begin(schema, context)


class CreateReferencedInheritingObject(inheriting.CreateInheritingObject):
    @classmethod
    def _cmd_tree_from_ast(cls, schema, astnode, context):
        cmd = super()._cmd_tree_from_ast(schema, astnode, context)

        if isinstance(astnode, cls.referenced_astnode):
            objcls = cls.get_schema_metaclass()

            try:
                base = utils.ast_to_typeref(
                    qlast.TypeName(maintype=astnode.name),
                    modaliases=context.modaliases, schema=schema)
            except s_err.ItemNotFoundError:
                # Certain concrete items, like pointers create
                # abstract parents implicitly.
                nname = objcls.get_shortname(cmd.classname)
                base = so.ObjectRef(
                    classname=sn.Name(
                        module=nname.module,
                        name=nname.name
                    )
                )

            cmd.add(
                sd.AlterObjectProperty(
                    property='bases',
                    new_value=so.ObjectList([base])
                )
            )

            referrer_ctx = cls.get_referrer_context(context)
            referrer_class = referrer_ctx.op.get_schema_metaclass()
            referrer_name = referrer_ctx.op.classname
            refdict = referrer_class.get_refdict_for_class(objcls)

            cmd.add(
                sd.AlterObjectProperty(
                    property=refdict.backref_attr,
                    new_value=so.ObjectRef(
                        classname=referrer_name
                    )
                )
            )

            if getattr(astnode, 'is_abstract', None):
                cmd.add(
                    sd.AlterObjectProperty(
                        property='is_abstract',
                        new_value=True
                    )
                )

        return cmd

    @classmethod
    def _classbases_from_ast(cls, schema, astnode, context):
        if isinstance(astnode, cls.referenced_astnode):
            # The bases will be populated by a call to derive()
            # from within _create_begin()
            bases = None
        else:
            bases = super()._classbases_from_ast(schema, astnode, context)

        return bases


class ReferencingObject(inheriting.InheritingObject,
                        metaclass=ReferencingObjectMeta):

    def copy_with(self, schema, updates):
        for refdict in self.__class__.get_refdicts():
            attr = refdict.attr
            local_attr = refdict.local_attr

            all_coll = self.get_explicit_field_value(schema, attr, None)
            if all_coll is None:
                updates[attr] = None
                updates[local_attr] = None
                continue

            all_coll_copy = {}
            for n, p in all_coll.items(schema):
                all_coll_copy[n] = p.copy()

            if all_coll_copy:
                updates[attr] = all_coll_copy
            else:
                updates[attr] = None
                updates[local_attr] = None
                continue

            local_coll = self.get_explicit_field_value(self, local_attr, None)
            if local_coll is None:
                updates[local_attr] = None
            else:
                updates[local_attr] = {n: all_coll_copy[n]
                                       for n in local_coll.names(schema)}

        schema, result = super().copy_with(schema, updates)
        return schema, result

    def merge(self, *objs, schema, dctx=None):
        schema = super().merge(*objs, schema=schema, dctx=None)

        for obj in objs:
            for refdict in self.__class__.get_refdicts():
                # Merge Object references in each registered collection.
                #
                this_coll = getattr(self, refdict.attr)
                other_coll = getattr(obj, refdict.attr)

                if other_coll is None:
                    continue

                if this_coll is None:
                    setattr(self, refdict.attr, other_coll)
                else:
                    updates = {k: v for k, v in other_coll.items(schema)
                               if not this_coll.has(schema, k)}

                    schema, this_coll = this_coll.replace(schema, updates)
                    setattr(self, refdict.attr, this_coll)

        return schema

    @classmethod
    def delta(cls, old, new, *, context=None, old_schema, new_schema):
        context = context or so.ComparisonContext()

        with context(old, new):
            delta = super().delta(old, new, context=context,
                                  old_schema=old_schema, new_schema=new_schema)
            if isinstance(delta, sd.CreateObject):
                # If this is a CREATE delta, we need to make
                # sure it is returned separately from the creation
                # of references, which will go into a separate ALTER
                # delta.  This is needed to avoid the hassle of
                # sorting the delta order by dependencies or having
                # to maintain ephemeral forward references.
                #
                # Generate an empty delta.
                alter_delta = super().delta(new, new, context=context,
                                            old_schema=new_schema,
                                            new_schema=new_schema)
                full_delta = sd.CommandGroup()
                full_delta.add(delta)
            else:
                full_delta = alter_delta = delta

            idx_key = lambda o: o.name

            for refdict in cls.get_refdicts():
                local_attr = refdict.local_attr

                if old:
                    oldcoll = getattr(old, local_attr).objects(old_schema)
                    oldcoll_idx = ordered.OrderedIndex(oldcoll, key=idx_key)
                else:
                    oldcoll_idx = {}

                if new:
                    newcoll = getattr(new, local_attr).objects(new_schema)
                    newcoll_idx = ordered.OrderedIndex(newcoll, key=idx_key)
                else:
                    newcoll_idx = {}

                cls.delta_sets(oldcoll_idx, newcoll_idx, alter_delta, context,
                               old_schema=old_schema, new_schema=new_schema)

            if alter_delta is not full_delta:
                if alter_delta.has_subcommands():
                    full_delta.add(alter_delta)
                else:
                    full_delta = delta

        return full_delta

    def get_classref_origin(self, schema, name, attr, local_attr, classname,
                            farthest=False):
        assert getattr(self, attr).has(schema, name)

        result = None

        if getattr(self, local_attr).has(schema, name):
            result = self

        if not result or farthest:
            bases = (c for c in self.get_mro()[1:]
                     if isinstance(c, named.NamedObject))

            for c in bases:
                if getattr(c, local_attr).has(schema, name):
                    result = c
                    if not farthest:
                        break

        if result is None:
            raise KeyError(
                'could not find {} "{}" origin'.format(classname, name))

        return result

    def add_classref(self, schema, collection, obj, replace=False):
        refdict = self.__class__.get_refdict(collection)
        attr = refdict.attr
        local_attr = refdict.local_attr

        local_coll = getattr(self, local_attr)
        all_coll = getattr(self, attr)

        key = obj.get_shortname(obj.name)

        if local_coll is not None:
            if local_coll.has(schema, key) and not replace:
                raise s_err.SchemaError(
                    f'{attr} {key!r} is already present in {self.name!r}',
                    context=obj.sourcectx)

        if local_coll is not None:
            schema, local_coll = local_coll.replace(schema, {key: obj})
            setattr(self, local_attr, local_coll)
        else:
            setattr(self, local_attr, so.ObjectMapping({key: obj}))

        if all_coll is not None:
            schema, all_coll = all_coll.replace(schema, {key: obj})
            setattr(self, attr, all_coll)
        else:
            setattr(self, attr, so.ObjectMapping({key: obj}))

        return schema

    def del_classref(self, schema, collection, obj_name):
        refdict = self.__class__.get_refdict(collection)
        attr = refdict.attr
        local_attr = refdict.local_attr
        refcls = refdict.ref_cls

        local_coll = getattr(self, local_attr)
        all_coll = getattr(self, attr)

        key = refcls.get_shortname(obj_name)
        is_inherited = any(getattr(b, attr).has(schema, key)
                           for b in self.bases)

        if not is_inherited:
            schema, all_coll = all_coll.replace(schema, {key: None})
            setattr(self, attr, all_coll)

            for descendant in self.descendants(schema):
                descendant_local_coll = getattr(descendant, local_attr)
                if not descendant_local_coll.has(schema, key):
                    descendant_coll = getattr(descendant, attr)
                    schema, descendant_coll = descendant_coll.replace(
                        schema, {key: None})
                    setattr(descendant, attr, descendant_coll)

        if local_coll and local_coll.has(schema, key):
            schema, local_coll = local_coll.replace(schema, {key: None})
            setattr(self, local_attr, local_coll)

        return schema

    def finalize(self, schema, bases=None, *, apply_defaults=True, dctx=None):
        schema = super().finalize(
            schema, bases=bases, apply_defaults=apply_defaults,
            dctx=dctx)

        if bases is None:
            bases = self.bases

        for refdict in self.__class__.get_refdicts():
            attr = refdict.attr
            local_attr = refdict.local_attr
            backref_attr = refdict.backref_attr
            ref_cls = refdict.ref_cls
            exp_inh = refdict.requires_explicit_inherit

            schema, ref_keys = self.begin_classref_dict_merge(
                schema, bases=bases, attr=attr)

            schema = self.merge_classref_dict(
                schema, bases=bases, attr=attr,
                local_attr=local_attr,
                backref_attr=backref_attr,
                classrefcls=ref_cls,
                classref_keys=ref_keys,
                requires_explicit_inherit=exp_inh,
                dctx=dctx)

            schema = self.finish_classref_dict_merge(
                schema, bases=bases, attr=attr)

        return schema

    def begin_classref_dict_merge(self, schema, bases, attr):
        return schema, None

    def finish_classref_dict_merge(self, schema, bases, attr):
        return schema

    def merge_classref_dict(self, schema, *,
                            bases, attr, local_attr,
                            backref_attr, classrefcls,
                            classref_keys, requires_explicit_inherit,
                            dctx=None):
        """Merge reference collections from bases.

        :param schema:         The schema.

        :param bases:          An iterable containing base objects.

        :param str attr:       Name of the attribute containing the full
                               reference collection.

        :param str local_attr: Name of the attribute containing the collection
                               of references defined locally (not inherited).

        :param str backref_attr: Name of the attribute on a referenced
                                 object containing the reference back to
                                 this object.

        :param classrefcls:    Referenced object class.

        :param classrefkeys:   An optional list of reference keys to consider
                               for merging.  If not specified, all keys
                               in the collection will be used.
        """
        classrefs = getattr(self, attr)
        if classrefs is None:
            classrefs = so.ObjectMapping()

        local_classrefs = getattr(self, local_attr)
        if local_classrefs is None:
            local_classrefs = so.ObjectMapping()

        if classref_keys is None:
            classref_keys = classrefs.names(schema)

        for classref_key in classref_keys:
            local = local_classrefs.get(schema, classref_key, None)

            inherited = []
            for b in bases:
                attrval = getattr(b, attr, {})
                if not attrval:
                    continue
                bref = attrval.get(schema, classref_key, None)
                if bref is not None:
                    inherited.append(bref)

            ancestry = {getattr(pref, backref_attr): pref
                        for pref in inherited}

            inherited = list(ancestry.values())

            pure_inheritance = False

            if local and inherited:
                schema, merged = local.derive_copy(
                    schema, self, merge_bases=inherited,
                    replace_original=local,
                    add_to_schema=True, dctx=dctx)

            elif len(inherited) > 1:
                base = inherited[0].bases[0]
                schema, merged = base.derive(
                    schema, self, merge_bases=inherited,
                    add_to_schema=True, dctx=dctx)

            elif len(inherited) == 1:
                # Pure inheritance
                item = inherited[0]
                # In some cases pure inheritance is not possible, such
                # as when a pointer has delegated constraints that must
                # be materialized on inheritance.  We delegate the
                # decision to the referenced class here.
                schema, merged = classrefcls.inherit_pure(
                    schema, item, source=self, dctx=dctx)
                pure_inheritance = merged is item

            else:
                # Not inherited
                merged = local

            if (local is not None and local is not merged and
                    requires_explicit_inherit and
                    not local.declared_inherited and
                    dctx is not None and dctx.declarative):
                # locally defined references *must* use
                # the `inherited` keyword if ancestors have
                # a reference under the same name.
                raise s_err.SchemaDefinitionError(
                    f'{self.shortname}: {local.shortname} must be '
                    f'declared using the `inherited` keyword because '
                    f'it is defined in the following ancestor(s): '
                    f'{", ".join(a.shortname for a in ancestry)}',
                    context=local.sourcectx
                )

            if merged is local and local.declared_inherited:
                raise s_err.SchemaDefinitionError(
                    f'{self.shortname}: {local.shortname} cannot '
                    f'be declared `inherited` as there are no ancestors '
                    f'defining it.',
                    context=local.sourcectx
                )

            if merged is not local:
                if not pure_inheritance:
                    if dctx is not None:
                        delta = merged.delta(local, merged,
                                             context=None,
                                             old_schema=schema,
                                             new_schema=schema)
                        if delta.has_subcommands():
                            dctx.current().op.add(delta)

                    schema, local_classrefs = local_classrefs.replace(
                        schema, {classref_key: merged})

                schema, classrefs = classrefs.replace(
                    schema, {classref_key: merged})

        setattr(self, attr, classrefs)
        setattr(self, local_attr, local_classrefs)

        return schema

    def init_derived(self, schema, source, *qualifiers, as_copy,
                     merge_bases=None, add_to_schema=False, mark_derived=False,
                     attrs=None, dctx=None, **kwargs):

        schema, derived = super().init_derived(
            schema, source, *qualifiers, as_copy=as_copy,
            mark_derived=mark_derived, add_to_schema=add_to_schema,
            attrs=attrs, dctx=dctx, merge_bases=merge_bases, **kwargs)

        if as_copy:
            schema = derived.rederive_classrefs(
                schema, add_to_schema=add_to_schema,
                mark_derived=mark_derived)

        return schema, derived

    def rederive_classrefs(self, schema, add_to_schema=False,
                           mark_derived=False, dctx=None):
        for refdict in self.__class__.get_refdicts():
            attr = refdict.attr
            local_attr = refdict.local_attr
            all_coll = getattr(self, attr)
            local_coll = getattr(self, local_attr)

            for pn, p in local_coll.items(schema):
                schema, obj = p.derive_copy(
                    schema, self,
                    add_to_schema=add_to_schema,
                    mark_derived=mark_derived,
                    dctx=dctx)

                schema, local_coll = local_coll.replace(
                    schema, {pn: obj})

            schema, all_coll = all_coll.replace(schema, local_coll)

            setattr(self, attr, all_coll)
            setattr(self, local_attr, local_coll)

        return schema


class ReferencingObjectCommand(sd.ObjectCommand):
    def _apply_fields_ast(self, schema, context, node):
        super()._apply_fields_ast(schema, context, node)

        mcls = self.get_schema_metaclass()

        for refdict in mcls.get_refdicts():
            self._apply_refs_fields_ast(schema, context, node, refdict)

    def _create_innards(self, schema, context):
        schema = super()._create_innards(schema, context)

        mcls = self.get_schema_metaclass()

        for refdict in mcls.get_refdicts():
            schema = self._create_refs(schema, context, self.scls, refdict)

        return schema

    def _alter_innards(self, schema, context, scls):
        schema = super()._alter_innards(schema, context, scls)

        mcls = self.get_schema_metaclass()

        for refdict in mcls.get_refdicts():
            schema = self._alter_refs(schema, context, scls, refdict)

        return schema

    def _delete_innards(self, schema, context, scls):
        schema = super()._delete_innards(schema, context, scls)

        mcls = self.get_schema_metaclass()

        for refdict in mcls.get_refdicts():
            schema = self._delete_refs(schema, context, scls, refdict)

        return schema

    def _apply_refs_fields_ast(self, schema, context, node, refdict):
        for op in self.get_subcommands(metaclass=refdict.ref_cls):
            self._append_subcmd_ast(schema, node, op, context)

    def _create_refs(self, schema, context, scls, refdict):
        for op in self.get_subcommands(metaclass=refdict.ref_cls):
            schema, _ = op.apply(schema, context=context)
        return schema

    def _alter_refs(self, schema, context, scls, refdict):
        for op in self.get_subcommands(metaclass=refdict.ref_cls):
            schema, _ = op.apply(schema, context=context)
        return schema

    def _delete_refs(self, schema, context, scls, refdict):
        deleted_refs = set()

        for op in self.get_subcommands(metaclass=refdict.ref_cls):
            schema, deleted_ref = op.apply(schema, context=context)
            deleted_refs.add(deleted_ref)

        # Add implicit Delete commands for any local refs not
        # deleted explicitly.
        all_refs = set(getattr(scls, refdict.local_attr).objects(schema))

        for ref in all_refs - deleted_refs:
            del_cmd = sd.ObjectCommandMeta.get_command_class(
                named.DeleteNamedObject, type(ref))

            op = del_cmd(classname=ref.name)
            schema, _ = op.apply(schema, context=context)
            self.add(op)

        return schema
