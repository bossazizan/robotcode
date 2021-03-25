import importlib
import importlib.util
import io
import os
import pkgutil
import re
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import (
    AbstractSet,
    Any,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Set,
    Tuple,
    Union,
    ValuesView,
    cast,
)

from pydantic import BaseModel, Field, PrivateAttr

from ...language_server.types import Position, Range
from ...utils.path import path_is_relative_to

__all__ = [
    "KeywordDoc",
    "LibraryDoc",
    "KeywordStore",
    "is_library_by_path",
    "get_library_doc",
    "find_file",
    "is_embedded_keyword",
    "complete_library_import",
]


RUN_KEYWORD_NAMES = [
    "Run Keyword",
    "Run Keyword And Continue On Failure",
    "Run Keyword And Ignore Error",
    "Run Keyword And Return",
    "Run Keyword And Return Status",
    "Run Keyword If All Critical Tests Passed",
    "Run Keyword If All Tests Passed",
    "Run Keyword If Any Critical Tests Failed",
    "Run Keyword If Any Tests Failed",
    "Run Keyword If Test Failed",
    "Run Keyword If Test Passed",
    "Run Keyword If Timeout Occurred",
]

RUN_KEYWORD_WITH_CONDITION_NAMES = ["Run Keyword And Expect Error", "Run Keyword And Return If", "Run Keyword Unless"]

RUN_KEYWORD_IF_NAME = "Run Keyword If"

RUN_KEYWORDS_NAME = "Run Keywords"

RUN_KEYWORDS = [*RUN_KEYWORD_NAMES, *RUN_KEYWORD_WITH_CONDITION_NAMES, RUN_KEYWORDS_NAME, RUN_KEYWORD_IF_NAME]

BUILTIN_LIBRARY_NAME = "BuiltIn"

DEFAULT_LIBRARIES = (BUILTIN_LIBRARY_NAME, "Reserved", "Easter")
ROBOT_LIBRARY_PACKAGE = "robot.libraries"

ALLOWED_LIBRARY_FILE_EXTENSIONS = [".py"]
ALLOWED_RESOURCE_FILE_EXTENSIONS = [".robot", ".resource", ".rst", ".rest", ".txt"]


def is_embedded_keyword(name: str) -> bool:
    from robot.errors import VariableError
    from robot.running.arguments.embedded import EmbeddedArguments

    try:
        if EmbeddedArguments(name):
            return True
    except VariableError:
        return True

    return False


class KeywordMatcher:
    def __init__(self, name: str) -> None:
        from robot.errors import VariableError
        from robot.running.arguments.embedded import EmbeddedArguments
        from robot.utils.normalizing import normalize

        self.name = name
        self.normalized_name = str(normalize(name, "_"))
        try:
            self.embedded_arguments = EmbeddedArguments(name)
        except VariableError:
            self.embedded_arguments = ()

    def __eq__(self, o: object) -> bool:
        from robot.utils.normalizing import normalize

        if not isinstance(o, str):
            return False

        if self.embedded_arguments:
            return self.embedded_arguments.name.match(o) is not None

        return self.normalized_name == str(normalize(o, "_"))

    def __hash__(self) -> int:
        return hash(self.name)

    def __str__(self) -> str:
        return self.name

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(name={repr(self.name)}"
            f", normalized_name={repr(self.normalized_name)}"
            f"{f', embedded=True' if self.embedded_arguments else ''})"
        )


RUN_KEYWORDS_MATCHERS = [KeywordMatcher(k) for k in RUN_KEYWORDS]
RUN_KEYWORD_IF_NAME_MATCHER = KeywordMatcher(RUN_KEYWORD_IF_NAME)
RUN_KEYWORDS_NAME_MATCHER = KeywordMatcher(RUN_KEYWORDS_NAME)
RUN_KEYWORD_NAMES_MATCHERS = [KeywordMatcher(k) for k in RUN_KEYWORD_NAMES]
RUN_KEYWORD_WITH_CONDITION_NAMES_MATCHERS = [KeywordMatcher(k) for k in RUN_KEYWORD_WITH_CONDITION_NAMES]


class Model(BaseModel):
    class Config:
        pass


class Error(Model):
    message: str
    type_name: str
    source: Optional[str] = None
    line_no: Optional[int] = None


class KeywordDoc(Model):
    name: str = ""
    args: Tuple[Any, ...] = ()
    doc: str = ""
    tags: Tuple[str, ...] = ()
    source: Optional[str] = None
    line_no: int = -1
    end_line_no: int = -1
    type: str = "keyword"
    libname: Optional[str] = None
    longname: Optional[str] = None
    is_embedded: bool = False
    errors: Optional[List[Error]] = None

    def __str__(self) -> str:
        return f"{self.name}({', '.join(str(arg) for arg in self.args)})"

    def range(self) -> Range:
        return Range(
            start=Position(line=self.line_no - 1 if self.line_no >= 0 else 0, character=0),
            end=Position(
                line=self.end_line_no - 1 if self.end_line_no >= 0 else self.line_no if self.line_no >= 0 else 0,
                character=0,
            ),
        )

    def to_markdown(self, add_signature: bool = True) -> str:
        if add_signature:
            result = "```python\n"

            result += f"({self.type}) \"{self.name}\": ({', '.join(self.args)})"

            result += "\n```"
        else:
            result = ""

        if self.doc:
            result += "\n"
            result += self.doc

        return result

    @property
    def signature(self) -> str:
        return f"({self.type}) \"{self.name}\": ({', '.join(self.args)})"

    @property
    def parameter_signature(self) -> str:
        return f"({', '.join(self.args)})"

    def is_any_run_keyword(self) -> bool:
        return self.libname == BUILTIN_LIBRARY_NAME and self.name in RUN_KEYWORDS

    def is_run_keyword(self) -> bool:
        return self.libname == BUILTIN_LIBRARY_NAME and self.name in RUN_KEYWORD_NAMES

    def is_run_keyword_with_condition(self) -> bool:
        return self.libname == BUILTIN_LIBRARY_NAME and self.name in RUN_KEYWORD_WITH_CONDITION_NAMES

    def is_run_keyword_if(self) -> bool:
        return self.libname == BUILTIN_LIBRARY_NAME and self.name == RUN_KEYWORD_IF_NAME

    def is_run_keywords(self) -> bool:
        return self.libname == BUILTIN_LIBRARY_NAME and self.name == RUN_KEYWORDS_NAME


class KeywordStore(Model):
    keywords: Dict[str, KeywordDoc] = Field(default_factory=lambda: {})
    __matchers: Optional[Dict[KeywordMatcher, KeywordDoc]] = PrivateAttr(None)

    @property
    def _matchers(self) -> Dict[KeywordMatcher, KeywordDoc]:
        if self.__matchers is None:
            self.__matchers = {KeywordMatcher(k): v for k, v in self.keywords.items()}
        return self.__matchers

    def __getitem__(self, key: str) -> "KeywordDoc":
        for k, v in self._matchers.items():
            if k == key:
                return v
        raise KeyError()

    def __contains__(self, __x: object) -> bool:
        return any(k == __x for k in self._matchers.keys())

    def __len__(self) -> int:
        return len(self.keywords)

    def items(self) -> AbstractSet[Tuple[str, KeywordDoc]]:
        return self.keywords.items()

    def keys(self) -> AbstractSet[str]:
        return self.keywords.keys()

    def values(self) -> ValuesView[KeywordDoc]:
        return self.keywords.values()

    def get(self, key: str, default: Optional[KeywordDoc] = None) -> Optional[KeywordDoc]:
        for k, v in self._matchers.items():
            if k == key:
                return v

        return default


class ModuleSpec(Model):
    name: str
    origin: Optional[str]
    submodule_search_locations: Optional[List[str]]


class LibraryDoc(Model):
    name: str = ""
    doc: str = ""
    version: str = ""
    type: str = "LIBRARY"
    scope: str = "TEST"
    named_args: bool = True
    doc_format: str = "ROBOT"
    source: Optional[str] = None
    line_no: int = -1
    end_line_no: int = -1
    inits: KeywordStore = KeywordStore()
    keywords: KeywordStore = KeywordStore()
    module_spec: Optional[ModuleSpec] = None
    errors: Optional[List[Error]] = None
    python_path: Optional[List[str]] = None
    stdout: Optional[str] = None

    def range(self) -> Range:
        return Range(
            start=Position(line=self.line_no - 1 if self.line_no >= 0 else 0, character=0),
            end=Position(
                line=self.end_line_no - 1 if self.end_line_no >= 0 else self.line_no if self.line_no >= 0 else 0,
                character=0,
            ),
        )

    def to_markdown(self, add_signature: bool = True) -> str:
        result = ""

        if add_signature:
            if self.inits:
                result += "\n\n---\n".join(i.to_markdown() for i in self.inits.values())
            else:
                result += "```python\n"
                result += f'({self.type.lower()}) "{self.name}": ()'
                result += "\n```"

        if self.doc:
            if result:
                result += "\n\n---\n"
            result += self.doc

        return result

    @property
    def python_source(self) -> Optional[str]:
        if self.source is not None:
            return self.source
        if self.module_spec is not None:
            if self.module_spec.origin is not None:
                return self.module_spec.origin

            if self.module_spec.submodule_search_locations:
                for e in self.module_spec.submodule_search_locations:
                    p = Path(e, "__init__.py")
                    if p.exists():
                        return str(p)

        return None


def is_library_by_path(path: str) -> bool:
    return path.lower().endswith((".py", ".java", ".class", "/", os.sep))


__PRELOADED_MODULES: Optional[Set[ModuleType]] = None


def _update_env(
    working_dir: str = ".", pythonpath: Optional[List[str]] = None, environment: Optional[Dict[str, str]] = None
) -> None:
    import gc

    global __PRELOADED_MODULES

    if __PRELOADED_MODULES is None:
        __PRELOADED_MODULES = set(sys.modules.values())
    else:
        for m in set(sys.modules.values()) - __PRELOADED_MODULES:
            try:
                importlib.reload(m)
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException:
                pass

    file = Path(__file__).resolve()
    top = file.parents[3]
    for p in filter(lambda v: path_is_relative_to(v, top), sys.path.copy()):
        sys.path.remove(p)
    wd = Path(working_dir)

    gc.collect()
    importlib.invalidate_caches()

    os.chdir(wd)

    if pythonpath is not None:
        for p in pythonpath:
            absolute_path = str(Path(p).absolute())
            if absolute_path not in sys.path:
                sys.path.insert(0, absolute_path)

    if environment:
        for k, v in environment.items():
            os.environ[k] = v

    try:
        # Try to reinitialize robot.running.context.EXECUTION_CONTEXTS to prevent exceptions
        # at reloading of libraries

        import robot.running.context

        robot.running.context.EXECUTION_CONTEXTS = robot.running.context.ExecutionContexts()
    except BaseException:
        pass


def get_module_spec(module_name: str) -> Optional[ModuleSpec]:
    import importlib.util

    result = None
    while result is None:
        try:
            result = importlib.util.find_spec(module_name)
        except BaseException:
            pass
        if result is None:
            splitted = module_name.rsplit(".", 1)
            if len(splitted) <= 1:
                break
            module_name = splitted[0]

    if result is not None:
        return ModuleSpec(
            name=result.name,
            origin=result.origin,
            submodule_search_locations=[i for i in result.submodule_search_locations]
            if result.submodule_search_locations
            else None,
        )
    return None


class KeywordWrapper:
    def __init__(self, kw: Any, source: str) -> None:
        self.kw = kw
        self.lib_source = source

    @property
    def name(self) -> Any:
        return self.kw.name

    @property
    def arguments(self) -> Any:
        return self.kw.arguments

    @property
    def doc(self) -> Any:
        try:
            return self.kw.doc
        except BaseException:
            return ""

    @property
    def tags(self) -> Any:
        try:
            return self.kw.tags
        except BaseException:
            return []

    @property
    def source(self) -> Any:
        try:
            return self.kw.source
        except BaseException:
            return self.lib_source

    @property
    def lineno(self) -> Any:
        try:
            return self.kw.lineno
        except BaseException:
            return 0

    @property
    def libname(self) -> Any:
        try:
            return self.kw.libname
        except BaseException:
            return ""

    @property
    def longname(self) -> Any:
        try:
            return self.kw.longname
        except BaseException:
            return ""


class Traceback(NamedTuple):
    source: str
    line_no: int


class MessageAndTraceback(NamedTuple):
    message: str
    traceback: List[Traceback]


__RE_MESSAGE = re.compile("^Traceback.*$", re.MULTILINE)
__RE_TRACEBACK = re.compile('^ +File +"(.*)", +line +([0-9]+).*$', re.MULTILINE)


def get_message_and_traceback_from_exception_text(text: str) -> MessageAndTraceback:
    splitted = __RE_MESSAGE.split(text, 1)

    return MessageAndTraceback(
        message=splitted[0].strip(),
        traceback=[Traceback(t.group(1), int(t.group(2))) for t in __RE_TRACEBACK.finditer(splitted[1])]
        if len(splitted) > 1
        else [],
    )


def error_from_exception(ex: BaseException, default_source: Optional[str], default_line_no: Optional[int]) -> Error:
    message_and_traceback = get_message_and_traceback_from_exception_text(str(ex))
    if message_and_traceback.traceback:
        tr = message_and_traceback.traceback[-1]
        return Error(
            message=str(ex),
            type_name=type(ex).__qualname__,
            source=tr.source,
            line_no=tr.line_no,
        )

    return Error(
        message=str(ex),
        type_name=type(ex).__qualname__,
        source=default_source,
        line_no=default_line_no,
    )


def init_builtin_variables(
    working_dir: str = ".", base_dir: str = ".", variables: Optional[Dict[str, Optional[Any]]] = None
) -> Dict[str, Optional[Any]]:
    result = {
        "${CURDIR}": str(Path(base_dir).absolute()),
        "${TEMPDIR}": str(Path(tempfile.gettempdir()).absolute()),
        "${EXECDIR}": str(Path(working_dir).absolute()),
        "${/}": os.sep,
        "${:}": os.pathsep,
        "${\\n}": os.linesep,
        "${SPACE}": " ",
        "${True}": True,
        "${False}": False,
        "${None}": None,
        "${null}": None,
        "${TEST NAME}": None,
        "@{TEST TAGS}": [],
        "${TEST DOCUMENTATION}": None,
        "${TEST STATUS}": None,
        "${TEST MESSAGE}": None,
        "${PREV TEST NAME}": None,
        "${PREV TEST STATUS}": None,
        "${PREV TEST MESSAGE}": None,
        "${SUITE NAME}": None,
        "${SUITE SOURCE}": None,
        "${SUITE DOCUMENTATION}": None,
        "&{SUITE METADATA}": {},
        "${SUITE STATUS}": None,
        "${SUITE MESSAGE}": None,
        "${KEYWORD STATUS}": None,
        "${KEYWORD MESSAGE}": None,
        "${LOG LEVEL}": None,
        "${OUTPUT FILE}": None,
        "${LOG FILE}": None,
        "${REPORT FILE}": None,
        "${DEBUG FILE}": None,
        "${OUTPUT DIR}": None,
    }
    if variables is not None:
        result.update((f"${{{k}}}", v) for k, v in variables.items())

    return result


@contextmanager
def _std_capture() -> Iterator[io.StringIO]:
    old__stdout__ = sys.__stdout__
    old__stderr__ = sys.__stderr__
    old_stdout = sys.stdout
    old_stderr = sys.stderr

    capturer = sys.stdout = sys.__stdout__ = sys.stderr = sys.__stderr__ = io.StringIO()

    try:
        yield capturer
    finally:
        sys.stderr = old_stderr
        sys.stdout = old_stdout
        sys.__stderr__ = old__stderr__
        sys.__stdout__ = old__stdout__


class IgnoreEasterEggLibraryWarning(BaseException):
    pass


def get_library_doc(
    name: str,
    args: Optional[Tuple[Any, ...]] = None,
    working_dir: str = ".",
    base_dir: str = ".",
    pythonpath: Optional[List[str]] = None,
    environment: Optional[Dict[str, str]] = None,
    variables: Optional[Dict[str, Optional[Any]]] = None,
) -> LibraryDoc:

    from robot.libdocpkg.robotbuilder import KeywordDocBuilder
    from robot.libraries import STDLIBS
    from robot.output import LOGGER
    from robot.running.outputcapture import OutputCapturer
    from robot.running.testlibraries import _get_lib_class
    from robot.utils import Importer
    from robot.utils.robotpath import find_file as robot_find_file
    from robot.variables import Variables

    def import_test_library(
        name: str,
    ) -> Union[Any, Tuple[Any, str]]:

        with OutputCapturer(library_import=True):
            importer = Importer("test library")
            return importer.import_class_or_module(name, return_source=True)

    def get_test_library(
        libcode: Any,
        source: str,
        name: str,
        args: Optional[Tuple[Any, ...]] = None,
        variables: Optional[Dict[str, Optional[Any]]] = None,
        create_handlers: bool = True,
        logger: Any = LOGGER,
    ) -> Any:
        libclass = _get_lib_class(libcode)
        lib = libclass(libcode, name, args or [], source, logger, variables)
        if create_handlers:
            lib.create_handlers()

        return lib

    with _std_capture() as std_capturer:
        _update_env(working_dir, pythonpath, environment)

        robot_variables = Variables()
        for k, v in init_builtin_variables(working_dir, base_dir, variables).items():
            robot_variables[k] = v

        name = robot_variables.replace_string(name.replace("\\", "\\\\"), ignore_errors=True)

        if name in STDLIBS:
            import_name = ROBOT_LIBRARY_PACKAGE + "." + name
        else:
            import_name = name

        module_spec: Optional[ModuleSpec] = None
        if is_library_by_path(import_name):
            import_name = robot_find_file(import_name, base_dir or ".", "Library")
        else:
            module_spec = get_module_spec(import_name)

        # skip antigravity easter egg
        # see https://python-history.blogspot.com/2010/06/import-antigravity.html
        if import_name.lower() in ["antigravity"] or import_name.lower().endswith(
            ("lib/antigravity.py", f"lib{os.sep}antigravity.py")
        ):
            raise IgnoreEasterEggLibraryWarning(f"Ignoring import for python easter egg '{import_name}'.")

        errors: List[Error] = []

        source = None
        try:
            libcode, source = import_test_library(import_name)
        except BaseException as e:
            return LibraryDoc(
                name=name,
                source=source,
                errors=[
                    error_from_exception(
                        e,
                        source or module_spec.origin if module_spec is not None else None,
                        1 if source is not None or module_spec is not None and module_spec.origin is not None else None,
                    )
                ],
                module_spec=module_spec,
                python_path=sys.path,
            )

        library_name = name
        library_name_path = Path(import_name)
        if library_name_path.exists():
            library_name = library_name_path.stem

        lib = None
        try:
            lib = get_test_library(
                libcode,
                source,
                name,
                args,
                create_handlers=False,
                variables=robot_variables,
            )
        except BaseException as e:
            errors.append(
                error_from_exception(
                    e,
                    source or module_spec.origin if module_spec is not None else None,
                    1 if source is not None or module_spec is not None and module_spec.origin is not None else None,
                )
            )

            if args:
                try:
                    lib = get_test_library(libcode, source, library_name, (), create_handlers=False)
                except BaseException:
                    pass

        libdoc = LibraryDoc(
            name=library_name,
            source=lib.source if lib is not None else source,
            module_spec=module_spec,
            python_path=sys.path,
        )

        if lib is not None:
            try:

                libdoc.inits = KeywordStore(
                    keywords={
                        kw[0].name: KeywordDoc(
                            name=libdoc.name,
                            args=tuple(str(a) for a in kw[0].args),
                            doc=kw[0].doc,
                            tags=tuple(kw[0].tags),
                            source=kw[0].source,
                            line_no=kw[0].lineno,
                            type="library",
                            libname=kw[1].libname,
                            longname=kw[1].longname,
                        )
                        for kw in [
                            (KeywordDocBuilder().build_keyword(k), k) for k in [KeywordWrapper(lib.init, source)]
                        ]
                    }
                )

                libdoc.line_no = lib.lineno
                libdoc.doc = str(lib.doc)
                libdoc.version = str(lib.version)
                libdoc.scope = str(lib.scope)
                libdoc.doc_format = str(lib.doc_format)

                # TODO: create logger to catch log messages at creating keywords
                lib.create_handlers()

                libdoc.keywords = KeywordStore(
                    keywords={
                        kw[0].name: KeywordDoc(
                            name=kw[0].name,
                            args=tuple(str(a) for a in kw[0].args),
                            doc=kw[0].doc,
                            tags=tuple(kw[0].tags),
                            source=kw[0].source,
                            line_no=kw[0].lineno,
                            libname=kw[1].libname,
                            longname=kw[1].longname,
                            is_embedded=is_embedded_keyword(kw[0].name),
                        )
                        for kw in [
                            (KeywordDocBuilder().build_keyword(k), k)
                            for k in [KeywordWrapper(k, source) for k in lib.handlers]
                        ]
                    }
                )

            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as e:
                errors.append(
                    error_from_exception(
                        e,
                        source or module_spec.origin if module_spec is not None else None,
                        1 if source is not None or module_spec is not None and module_spec.origin is not None else None,
                    )
                )

        if errors:
            libdoc.errors = errors

    libdoc.stdout = std_capturer.getvalue()

    return libdoc


def find_file(
    name: str,
    working_dir: str = ".",
    base_dir: str = ".",
    pythonpath: Optional[List[str]] = None,
    environment: Optional[Dict[str, str]] = None,
    variables: Optional[Dict[str, Optional[Any]]] = None,
    file_type: str = "Resource",
) -> str:
    from robot.utils.robotpath import find_file as robot_find_file
    from robot.variables import Variables

    _update_env(working_dir, pythonpath, environment)

    robot_variables = Variables()
    for k, v in init_builtin_variables(working_dir, base_dir, variables).items():
        robot_variables[k] = v

    name = robot_variables.replace_string(name.replace("\\", "\\\\"), ignore_errors=True)

    return cast(str, robot_find_file(name, base_dir or ".", file_type))


class CompleteResult(NamedTuple):
    label: str
    detail: str


def is_file_like(name: Optional[str]) -> bool:
    return name is not None and (
        name.startswith(".") or name.startswith("/") or name.startswith(os.sep) or "/" in name or os.sep in name
    )


def iter_module_names(name: Optional[str] = None) -> Iterator[str]:
    if name is not None:
        spec = importlib.util.find_spec(name)
        if spec is None:
            return
    else:
        spec = None

    if spec is None:
        for e in pkgutil.iter_modules():
            if not e.name.startswith(("_", ".")):
                yield e.name
        return

    if spec.submodule_search_locations is None:
        return

    for e in pkgutil.iter_modules(spec.submodule_search_locations):
        if not e.name.startswith(("_", ".")):
            yield e.name


def iter_modules_from_python_path(path: Optional[str] = None) -> Iterator[CompleteResult]:
    allow_modules = True if not path or not ("/" in path or os.sep in path) else False
    allow_files = True if not path or "/" in path or os.sep in path else False

    path = path.replace(".", os.sep) if path is not None and not path.startswith((".", "/", os.sep)) else path

    if path is None:
        paths = sys.path
    else:
        paths = [str(Path(s, path)) for s in sys.path]

    for e in [Path(p) for p in set(paths)]:
        if e.is_dir():
            for f in e.iterdir():
                if not f.name.startswith(("_", ".")) and (
                    f.is_file()
                    and f.suffix in ALLOWED_LIBRARY_FILE_EXTENSIONS
                    or f.is_dir()
                    and f.suffix not in [".dist-info"]
                ):
                    if f.is_dir():
                        yield CompleteResult(f.name, "Module")

                    if f.is_file():
                        if allow_modules:
                            yield CompleteResult(f.stem, "Module")
                        if allow_files:
                            yield CompleteResult(f.name, "File")


def iter_resources_from_python_path(path: Optional[str] = None) -> Iterator[CompleteResult]:
    if path is None:
        paths = sys.path
    else:
        paths = [str(Path(s, path)) for s in sys.path]

    for e in [Path(p) for p in set(paths)]:
        if e.is_dir():
            for f in e.iterdir():
                if not f.name.startswith(("_", ".")) and (
                    f.is_file()
                    and f.suffix in ALLOWED_RESOURCE_FILE_EXTENSIONS
                    or f.is_dir()
                    and f.suffix not in [".dist-info"]
                ):
                    yield CompleteResult(f.name, "Resource" if f.is_file() else "Directory")


def complete_library_import(
    name: Optional[str],
    working_dir: str = ".",
    base_dir: str = ".",
    pythonpath: Optional[List[str]] = None,
    environment: Optional[Dict[str, str]] = None,
    variables: Optional[Dict[str, Optional[Any]]] = None,
) -> Optional[List[CompleteResult]]:
    from robot.variables import Variables

    _update_env(working_dir, pythonpath, environment)

    result: List[CompleteResult] = []

    if name is None:
        result += [
            CompleteResult(e, "Module (Internal)")
            for e in iter_module_names(ROBOT_LIBRARY_PACKAGE)
            if e not in DEFAULT_LIBRARIES
        ]

    if name is not None:
        robot_variables = Variables()
        for k, v in init_builtin_variables(working_dir, base_dir, variables).items():
            robot_variables[k] = v

        name = robot_variables.replace_string(name.replace("\\", "\\\\"), ignore_errors=True)

    if name is None or not name.startswith((".", "/", os.sep)):
        result += [e for e in iter_modules_from_python_path(name)]

    if name is None or (is_file_like(name) and (name.endswith("/") or name.endswith(os.sep))):
        name_path = Path(name if name else base_dir)
        if name_path.is_absolute():
            path = name_path.resolve()
        else:
            path = Path(base_dir, name if name else base_dir).resolve()

        if path.exists() and path.is_dir():
            result += [
                CompleteResult(str(f.name), "File" if f.is_file() else "Directory")
                for f in path.iterdir()
                if not f.name.startswith(("_", "."))
                and (f.is_dir() or (f.is_file and f.suffix in ALLOWED_LIBRARY_FILE_EXTENSIONS))
            ]

    return list(set(result))


def complete_resource_import(
    name: Optional[str],
    working_dir: str = ".",
    base_dir: str = ".",
    pythonpath: Optional[List[str]] = None,
    environment: Optional[Dict[str, str]] = None,
    variables: Optional[Dict[str, Optional[Any]]] = None,
) -> Optional[List[CompleteResult]]:
    from robot.variables import Variables

    _update_env(working_dir, pythonpath, environment)

    result: List[CompleteResult] = []

    if name is not None:
        robot_variables = Variables()
        for k, v in init_builtin_variables(working_dir, base_dir, variables).items():
            robot_variables[k] = v

        name = robot_variables.replace_string(name.replace("\\", "\\\\"), ignore_errors=True)

    if name is None or not name.startswith(".") and not name.startswith("/") and not name.startswith(os.sep):
        result += [e for e in iter_resources_from_python_path(name)]

    if name is None or name.startswith(".") or name.startswith("/") or name.startswith(os.sep):
        name_path = Path(name if name else base_dir)
        if name_path.is_absolute():
            path = name_path.resolve()
        else:
            path = Path(base_dir, name if name else base_dir).resolve()

        if path.exists() and (path.is_dir()):
            result += [
                CompleteResult(str(f.name), "Resource" if f.is_file() else "Directory")
                for f in path.iterdir()
                if not f.name.startswith(("_", "."))
                and (f.is_dir() or (f.is_file and f.suffix in ALLOWED_RESOURCE_FILE_EXTENSIONS))
            ]

    return list(set(result))


def init_pool() -> None:
    """Dummy function to initialize the ProcessPollExecutor"""
    pass
