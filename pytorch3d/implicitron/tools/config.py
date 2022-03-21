# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


import copy
import dataclasses
import inspect
import warnings
from collections import Counter, defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Type, TypeVar, cast

from omegaconf import DictConfig, OmegaConf, open_dict


"""
This functionality allows a configurable system to be determined in a dataclass-type
way. It is a generalization of omegaconf's "structured", in the dataclass case.
Core functionality:

- Configurable -- A base class used to label a class as being one which uses this
                    system. Uses class members and __post_init__ like a dataclass.

- expand_args_fields -- Expands a class like `dataclasses.dataclass`. Runs automatically.

- get_default_args -- gets an omegaconf.DictConfig for initializing
                       a given class or calling a given function.

- run_auto_creation -- Initialises nested members. To be called in __post_init__.


In addition, a Configurable may contain members whose type is decided at runtime.

- ReplaceableBase -- As a base instead of Configurable, labels a class to say that
                     any child class can be used instead.

- registry -- A global store of named child classes of  ReplaceableBase classes.
              Used as `@registry.register` decorator on class definition.


Additional utility functions:

- remove_unused_components -- used for simplifying a DictConfig instance.
- get_default_args_field -- default for DictConfig member of another configurable.


1. The simplest usage of this functionality is as follows. First a schema is defined
in dataclass style.

    class A(Configurable):
        n: int = 9

    class B(Configurable):
        a: A

        def __post_init__(self):
            run_auto_creation(self)

It can be used like

    b_args = get_default_args(B)
    b = B(**b_args)

In this case, get_default_args(B) returns an omegaconf.DictConfig with the right
members {"a_args": {"n": 9}}. It also modifies the definitions of the classes to
something like the following. (The modification itself is done by the function
`expand_args_fields`, which is called inside `get_default_args`.)

    @dataclasses.dataclass
    class A:
        n: int = 9

    @dataclasses.dataclass
    class B:
        a_args: DictConfig = dataclasses.field(default_factory=lambda: DictConfig({"n": 9}))

        def __post_init__(self):
            self.a = A(**self.a_args)

2. Pluggability. Instead of a dataclass-style member being given a concrete class,
you can give a base class and the implementation is looked up by name in the global
`registry` in this module. E.g.

    class A(ReplaceableBase):
        k: int = 1

    @registry.register
    class A1(A):
        m: int = 3

    @registry.register
    class A2(A):
        n: str = "2"

    class B(Configurable):
        a: A
        a_class_type: str = "A2"

        def __post_init__(self):
            run_auto_creation(self)

will expand to

    @dataclasses.dataclass
    class A:
        k: int = 1

    @dataclasses.dataclass
    class A1(A):
        m: int = 3

    @dataclasses.dataclass
    class A2(A):
        n: str = "2"

    @dataclasses.dataclass
    class B:
        a_class_type: str = "A2"
        a_A1_args: DictConfig = dataclasses.field(
            default_factory=lambda: DictConfig({"k": 1, "m": 3}
        )
        a_A2_args: DictConfig = dataclasses.field(
            default_factory=lambda: DictConfig({"k": 1, "m": 3}
        )

        def __post_init__(self):
            if self.a_class_type == "A1":
                self.a = A1(**self.a_A1_args)
            elif self.a_class_type == "A2":
                self.a = A2(**self.a_A2_args)
            else:
                raise ValueError(...)

3. Aside from these classes, the members of these classes should be things
which DictConfig is happy with: e.g. (bool, int, str, None, float) and what
can be built from them with DictConfigs and lists of them.

In addition, you can call get_default_args on a function or class to get
the DictConfig of its defaulted arguments, assuming those are all things
which DictConfig is happy with. If you want to use such a thing as a member
of another configured class, `get_default_args_field` is a helper.
"""


_unprocessed_warning: str = (
    " must be processed before it can be used."
    + " This is done by calling expand_args_fields "
    + "or get_default_args on it."
)

TYPE_SUFFIX: str = "_class_type"
ARGS_SUFFIX: str = "_args"


class ReplaceableBase:
    """
    Base class for dataclass-style classes which
    can be stored in the registry.
    """

    def __new__(cls, *args, **kwargs):
        """
        This function only exists to raise a
        warning if class construction is attempted
        without processing.
        """
        obj = super().__new__(cls)
        if cls is not ReplaceableBase and not _is_actually_dataclass(cls):
            warnings.warn(cls.__name__ + _unprocessed_warning)
        return obj


class Configurable:
    """
    This indicates a class which is not ReplaceableBase
    but still needs to be
    expanded into a dataclass with expand_args_fields.
    This expansion is delayed.
    """

    def __new__(cls, *args, **kwargs):
        """
        This function only exists to raise a
        warning if class construction is attempted
        without processing.
        """
        obj = super().__new__(cls)
        if cls is not Configurable and not _is_actually_dataclass(cls):
            warnings.warn(cls.__name__ + _unprocessed_warning)
        return obj


_X = TypeVar("X", bound=ReplaceableBase)


class _Registry:
    """
    Register from names to classes. In particular, we say that direct subclasses of
    ReplaceableBase are "base classes" and we register subclasses of each base class
    in a separate namespace.
    """

    def __init__(self) -> None:
        self._mapping: Dict[
            Type[ReplaceableBase], Dict[str, Type[ReplaceableBase]]
        ] = defaultdict(dict)

    def register(self, some_class: Type[_X]) -> Type[_X]:
        """
        A class decorator, to register a class in self.
        """
        name = some_class.__name__
        self._register(some_class, name=name)
        return some_class

    def _register(
        self,
        some_class: Type[ReplaceableBase],
        *,
        base_class: Optional[Type[ReplaceableBase]] = None,
        name: str,
    ) -> None:
        """
        Register a new member.

        Args:
            cls: the new member
            base_class: (optional) what the new member is a type for
            name: name for the new member
        """
        if base_class is None:
            base_class = self._base_class_from_class(some_class)
            if base_class is None:
                raise ValueError(
                    f"Cannot register {some_class}. Cannot tell what it is."
                )
        if some_class is base_class:
            raise ValueError(f"Attempted to register the base class {some_class}")
        self._mapping[base_class][name] = some_class

    def get(
        self, base_class_wanted: Type[ReplaceableBase], name: str
    ) -> Type[ReplaceableBase]:
        """
        Retrieve a class from the registry by name

        Args:
            base_class_wanted: parent type of type we are looking for.
                        It determines the namespace.
                        This will typically be a direct subclass of ReplaceableBase.
            name: what to look for

        Returns:
            class type
        """
        if self._is_base_class(base_class_wanted):
            base_class = base_class_wanted
        else:
            base_class = self._base_class_from_class(base_class_wanted)
            if base_class is None:
                raise ValueError(
                    f"Cannot look up {base_class_wanted}. Cannot tell what it is."
                )
        result = self._mapping[base_class].get(name)
        if result is None:
            raise ValueError(f"{name} has not been registered.")
        if not issubclass(result, base_class_wanted):
            raise ValueError(
                f"{name} resolves to {result} which does not subclass {base_class_wanted}"
            )
        return result

    def get_all(
        self, base_class_wanted: Type[ReplaceableBase]
    ) -> List[Type[ReplaceableBase]]:
        """
        Retrieve all registered implementations from the registry

        Args:
            base_class_wanted: parent type of type we are looking for.
                        It determines the namespace.
                        This will typically be a direct subclass of ReplaceableBase.
        Returns:
            list of class types
        """
        if self._is_base_class(base_class_wanted):
            return list(self._mapping[base_class_wanted].values())

        base_class = self._base_class_from_class(base_class_wanted)
        if base_class is None:
            raise ValueError(
                f"Cannot look up {base_class_wanted}. Cannot tell what it is."
            )
        return [
            class_
            for class_ in self._mapping[base_class].values()
            if issubclass(class_, base_class_wanted) and class_ is not base_class_wanted
        ]

    @staticmethod
    def _is_base_class(some_class: Type[ReplaceableBase]) -> bool:
        """
        Return whether the given type is a direct subclass of ReplaceableBase
        and so gets used as a namespace.
        """
        return ReplaceableBase in some_class.__bases__

    @staticmethod
    def _base_class_from_class(
        some_class: Type[ReplaceableBase],
    ) -> Optional[Type[ReplaceableBase]]:
        """
        Find the parent class of some_class which inherits ReplaceableBase, or None
        """
        for base in some_class.mro()[-3::-1]:
            if base is not ReplaceableBase and issubclass(base, ReplaceableBase):
                return base
        return None


# Global instance of the registry
registry = _Registry()


def _default_create(name: str, type_: Type, pluggable: bool) -> Callable[[Any], None]:
    """
    Return the default creation function for a member. This is a function which
    could be called in __post_init__ to initialise the member, and will be called
    from run_auto_creation.

    Args:
        name: name of the member
        type_: declared type of the member
        pluggable: True if the member's declared type inherits ReplaceableBase,
                    in which case the actual type to be created is decided at
                    runtime.

    Returns:
        Function taking one argument, the object whose member should be
            initialized.
    """

    def inner(self):
        expand_args_fields(type_)
        args = getattr(self, name + ARGS_SUFFIX)
        setattr(self, name, type_(**args))

    def inner_pluggable(self):
        type_name = getattr(self, name + TYPE_SUFFIX)
        chosen_class = registry.get(type_, type_name)
        if self._known_implementations.get(type_name, chosen_class) is not chosen_class:
            # If this warning is raised, it means that a new definition of
            # the chosen class has been registered since our class was processed
            # (i.e. expanded). A DictConfig which comes from our get_default_args
            # (which might have triggered the processing) will contain the old default
            # values for the members of the chosen class. Changes to those defaults which
            # were made in the redefinition will not be reflected here.
            warnings.warn(f"New implementation of {type_name} is being chosen.")
        expand_args_fields(chosen_class)
        args = getattr(self, f"{name}_{type_name}{ARGS_SUFFIX}")
        setattr(self, name, chosen_class(**args))

    return inner_pluggable if pluggable else inner


def run_auto_creation(self: Any) -> None:
    """
    Run all the functions named in self._creation_functions.
    """
    for create_function in self._creation_functions:
        getattr(self, create_function)()


def _is_configurable_class(C) -> bool:
    return isinstance(C, type) and issubclass(C, (Configurable, ReplaceableBase))


def get_default_args(C, *, _do_not_process: Tuple[type, ...] = ()) -> DictConfig:
    """
    Get the DictConfig of args to call C - which might be a type or a function.

    If C is a subclass of Configurable or ReplaceableBase, we make sure
    it has been processed with expand_args_fields. If C is a dataclass,
    including a subclass of Configurable or ReplaceableBase, the output
    will be a typed DictConfig.

    Args:
        C: the class or function to be processed
        _do_not_process: (internal use) When this function is called from
                    expand_args_fields, we specify any class currently being
                    processed, to make sure we don't try to process a class
                    while it is already being processed.

    Returns:
        new DictConfig object
    """
    if C is None:
        return DictConfig({})

    if _is_configurable_class(C):
        if C in _do_not_process:
            raise ValueError(
                f"Internal recursion error. Need processed {C},"
                f" but cannot get it. _do_not_process={_do_not_process}"
            )
        # This is safe to run multiple times. It will return
        # straight away if C has already been processed.
        expand_args_fields(C, _do_not_process=_do_not_process)

    kwargs = {}
    if dataclasses.is_dataclass(C):
        # Note that if get_default_args_field is used somewhere in C,
        # this call is recursive. No special care is needed,
        # because in practice get_default_args_field is used for
        # separate types than the outer type.
        out = OmegaConf.structured(C)
        exclude = getattr(C, "_processed_members", ())
        with open_dict(out):
            for field in exclude:
                out.pop(field, None)
        return out

    if _is_configurable_class(C):
        raise ValueError(f"Failed to process {C}")

    # returns dict of keyword args of a callable C
    sig = inspect.signature(C)
    for pname, defval in dict(sig.parameters).items():
        if defval.default == inspect.Parameter.empty:
            # print('skipping %s' % pname)
            continue
        else:
            kwargs[pname] = copy.deepcopy(defval.default)

    return DictConfig(kwargs)


def _is_actually_dataclass(some_class) -> bool:
    # Return whether the class some_class has been processed with
    # the dataclass annotation. This is more specific than
    # dataclasses.is_dataclass which returns True on anything
    # deriving from a dataclass.

    # Checking for __init__ would also work for our purpose.
    return "__dataclass_fields__" in some_class.__dict__


def expand_args_fields(
    some_class: Type[_X], *, _do_not_process: Tuple[type, ...] = ()
) -> Type[_X]:
    """
    This expands a class which inherits Configurable or ReplaceableBase classes,
    including dataclass processing. some_class is modified in place by this function.
    For classes of type ReplaceableBase, you can add some_class to the registry before
    or after calling this function. But potential inner classes need to be registered
    before this function is run on the outer class.

    The transformations this function makes, before the concluding
    dataclasses.dataclass, are as follows.  if X is a base class with registered
    subclasses Y and Z, replace

        x: X

    and optionally

        x_class_type: str = "Y"
        def create_x(self):...

    with

        x_Y_args : DictConfig = dataclasses.field(default_factory=lambda: DictConfig())
        x_Z_args : DictConfig = dataclasses.field(default_factory=lambda: DictConfig())
        def create_x(self):
            self.x = registry.get(X, self.x_class_type)(
                **self.getattr(f"x_{self.x_class_type}_args)
            )
        x_class_type: str = "UNDEFAULTED"

    without adding the optional things if they are already there.

    Similarly, if X is a subclass of Configurable,

        x: X

    and optionally

        def create_x(self):...

    will be replaced with

        x_args : DictConfig = dataclasses.field(default_factory=lambda: DictConfig())
        def create_x(self):
            self.x = X(self.x_args)

    Also adds the following class members, unannotated so that dataclass
    ignores them.
        - _creation_functions: Tuple[str] of all the create_ functions,
            including those from base classes.
        - _known_implementations: Dict[str, Type] containing the classes which
            have been found from the registry.
            (used only to raise a warning if it one has been overwritten)
        - _processed_members: a Set[str] of all the members which have been transformed.

    Args:
        some_class: the class to be processed
        _do_not_process: Internal use for get_default_args: Because get_default_args calls
                        and is called by this function, we let it specify any class currently
                        being processed, to make sure we don't try to process a class while
                        it is already being processed.


    Returns:
        some_class itself, which has been modified in place. This
        allows this function to be used as a class decorator.
    """
    if _is_actually_dataclass(some_class):
        return some_class

    # The functions this class's run_auto_creation will run.
    creation_functions: List[str] = []
    # The classes which this type knows about from the registry
    # We could use a weakref.WeakValueDictionary here which would mean
    # that we don't warn if the class we should have expected is elsewhere
    # unused.
    known_implementations: Dict[str, Type] = {}
    # Names of members which have been processed.
    processed_members: Set[str] = set()

    # For all bases except ReplaceableBase and Configurable and object,
    # we need to process them before our own processing. This is
    # because dataclasses expect to inherit dataclasses and not unprocessed
    # dataclasses.
    for base in some_class.mro()[-3:0:-1]:
        if base is ReplaceableBase:
            continue
        if base is Configurable:
            continue
        if not issubclass(base, (Configurable, ReplaceableBase)):
            continue
        expand_args_fields(base, _do_not_process=_do_not_process)
        if "_creation_functions" in base.__dict__:
            creation_functions.extend(base._creation_functions)
        if "_known_implementations" in base.__dict__:
            known_implementations.update(base._known_implementations)
        if "_processed_members" in base.__dict__:
            processed_members.update(base._processed_members)

    to_process: List[Tuple[str, Type, bool]] = []
    if "__annotations__" in some_class.__dict__:
        for name, type_ in some_class.__annotations__.items():
            if not isinstance(type_, type):
                # type_ could be something like typing.Tuple
                continue
            if (
                issubclass(type_, ReplaceableBase)
                and ReplaceableBase in type_.__bases__
            ):
                to_process.append((name, type_, True))
            elif issubclass(type_, Configurable):
                to_process.append((name, type_, False))

    for name, type_, pluggable in to_process:
        _process_member(
            name=name,
            type_=type_,
            pluggable=pluggable,
            some_class=cast(type, some_class),
            creation_functions=creation_functions,
            _do_not_process=_do_not_process,
            known_implementations=known_implementations,
        )
        processed_members.add(name)

    for key, count in Counter(creation_functions).items():
        if count > 1:
            warnings.warn(f"Clash with {key} in a base class.")
    some_class._creation_functions = tuple(creation_functions)
    some_class._processed_members = processed_members
    some_class._known_implementations = known_implementations

    dataclasses.dataclass(eq=False)(some_class)
    return some_class


def get_default_args_field(C, *, _do_not_process: Tuple[type, ...] = ()):
    """
    Get a dataclass field which defaults to get_default_args(...)

    Args:
        As for get_default_args.

    Returns:
        function to return new DictConfig object
    """

    def create():
        return get_default_args(C, _do_not_process=_do_not_process)

    return dataclasses.field(default_factory=create)


def _process_member(
    *,
    name: str,
    type_: Type,
    pluggable: bool,
    some_class: Type,
    creation_functions: List[str],
    _do_not_process: Tuple[type, ...],
    known_implementations: Dict[str, Type],
) -> None:
    """
    Make the modification (of expand_args_fields) to some_class for a single member.

    Args:
        name: member name
        type_: member declared type
        plugglable: whether member has dynamic type
        some_class: (MODIFIED IN PLACE) the class being processed
        creation_functions: (MODIFIED IN PLACE) the names of the create functions
        _do_not_process: as for expand_args_fields.
        known_implementations: (MODIFIED IN PLACE) known types from the registry
    """
    # Because we are adding defaultable members, make
    # sure they go at the end of __annotations__ in case
    # there are non-defaulted standard class members.
    del some_class.__annotations__[name]

    if pluggable:
        type_name = name + TYPE_SUFFIX
        if type_name not in some_class.__annotations__:
            some_class.__annotations__[type_name] = str
            setattr(some_class, type_name, "UNDEFAULTED")

        for derived_type in registry.get_all(type_):
            if derived_type in _do_not_process:
                continue
            if issubclass(derived_type, some_class):
                # When derived_type is some_class we have a simple
                # recursion to avoid. When it's a strict subclass the
                # situation is even worse.
                continue
            known_implementations[derived_type.__name__] = derived_type
            args_name = f"{name}_{derived_type.__name__}{ARGS_SUFFIX}"
            if args_name in some_class.__annotations__:
                raise ValueError(
                    f"Cannot generate {args_name} because it is already present."
                )
            some_class.__annotations__[args_name] = DictConfig
            setattr(
                some_class,
                args_name,
                get_default_args_field(
                    derived_type, _do_not_process=_do_not_process + (some_class,)
                ),
            )
    else:
        args_name = name + ARGS_SUFFIX
        if args_name in some_class.__annotations__:
            raise ValueError(
                f"Cannot generate {args_name} because it is already present."
            )
        if issubclass(type_, some_class) or type_ in _do_not_process:
            raise ValueError(f"Cannot process {type_} inside {some_class}")

        some_class.__annotations__[args_name] = DictConfig
        setattr(
            some_class,
            args_name,
            get_default_args_field(
                type_,
                _do_not_process=_do_not_process + (some_class,),
            ),
        )

    creation_function_name = f"create_{name}"
    if not hasattr(some_class, creation_function_name):
        setattr(
            some_class,
            creation_function_name,
            _default_create(name, type_, pluggable),
        )
    creation_functions.append(creation_function_name)


def remove_unused_components(dict_: DictConfig) -> None:
    """
    Assuming dict_ represents the state of a configurable,
    modify it to remove all the portions corresponding to
    pluggable parts which are not in use.
    For example, if renderer_class_type is SignedDistanceFunctionRenderer,
    the renderer_MultiPassEmissionAbsorptionRenderer_args will be
    removed.

    Args:
        dict_: (MODIFIED IN PLACE) a DictConfig instance
    """
    keys = [key for key in dict_ if isinstance(key, str)]
    suffix_length = len(TYPE_SUFFIX)
    replaceables = [key[:-suffix_length] for key in keys if key.endswith(TYPE_SUFFIX)]
    args_keys = [key for key in keys if key.endswith(ARGS_SUFFIX)]
    for replaceable in replaceables:
        selected_type = dict_[replaceable + TYPE_SUFFIX]
        expect = replaceable + "_" + selected_type + ARGS_SUFFIX
        with open_dict(dict_):
            for key in args_keys:
                if key.startswith(replaceable + "_") and key != expect:
                    del dict_[key]

    for key in dict_:
        if isinstance(dict_.get(key), DictConfig):
            remove_unused_components(dict_[key])
