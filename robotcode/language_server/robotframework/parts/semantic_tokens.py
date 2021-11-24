from __future__ import annotations

import ast
import asyncio
import operator
import re
from dataclasses import dataclass
from enum import Enum
from functools import reduce
from typing import (
    TYPE_CHECKING,
    Any,
    AsyncGenerator,
    Dict,
    FrozenSet,
    List,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)

from ....utils.async_event import CancelationToken
from ....utils.async_itertools import async_dropwhile, async_takewhile
from ....utils.logging import LoggingDescriptor
from ...common.language import language_id
from ...common.lsp_types import (
    Range,
    SemanticTokenModifiers,
    SemanticTokens,
    SemanticTokensDelta,
    SemanticTokensDeltaPartialResult,
    SemanticTokensPartialResult,
    SemanticTokenTypes,
)
from ...common.text_document import TextDocument
from ..diagnostics.library_doc import (
    ALL_RUN_KEYWORDS_MATCHERS,
    BUILTIN_LIBRARY_NAME,
    KeywordDoc,
    KeywordMatcher,
    LibraryDoc,
)
from ..diagnostics.namespace import Namespace
from ..utils.ast import (
    HasTokens,
    Token,
    is_not_variable_token,
    iter_nodes,
    token_in_range,
    tokenize_variables,
    yield_owner_and_kw_names,
)

if TYPE_CHECKING:
    from ..protocol import RobotLanguageServerProtocol

from .protocol_part import RobotLanguageServerProtocolPart

ROBOT_KEYWORD_INNER = "KEYWORD_INNER"


class RobotSemTokenTypes(Enum):
    SECTION = "section"
    SETTING_IMPORT = "settingImport"
    SETTING = "setting"
    HEADER = "header"
    HEADER_SETTING = "headerSetting"
    HEADER_VARIABLE = "headerVariable"
    HEADER_TESTCASE = "headerTestcase"
    HEADER_COMMENT = "headerComment"
    HEADER_KEYWORD = "headerKeyword"
    TESTCASE_NAME = "testcaseName"
    KEYWORD_NAME = "keywordName"
    CONTROL_FLOW = "controlFlow"
    ARGUMENT = "argument"
    VARIABLE = "variable"
    KEYWORD = "keywordCall"
    KEYWORD_INNER = "keywordCallInner"
    NAME = "nameCall"
    CONTINUATION = "continuation"
    SEPARATOR = "separator"
    TERMINATOR = "terminator"
    FOR_SEPARATOR = "forSeparator"
    VARIABLE_BEGIN = "variableBegin"
    VARIABLE_END = "variableEnd"
    ESCAPE = "escape"
    NAMESPACE = "namespace"


class RobotSemTokenModifiers(Enum):
    BUILTIN = "builtin"


@dataclass
class SemTokenInfo:
    lineno: int
    col_offset: int
    length: int
    sem_token_type: Enum
    sem_modifiers: Optional[Set[Enum]] = None

    @classmethod
    def from_token(
        cls,
        token: Token,
        sem_token_type: Enum,
        sem_modifiers: Optional[Set[Enum]] = None,
        col_offset: Optional[int] = None,
        length: Optional[int] = None,
    ) -> SemTokenInfo:
        return cls(
            token.lineno,
            col_offset if col_offset is not None else token.col_offset,
            length if length is not None else token.end_col_offset - token.col_offset,
            sem_token_type,
            sem_modifiers,
        )


class RobotSemanticTokenProtocolPart(RobotLanguageServerProtocolPart):
    _logger = LoggingDescriptor()

    def __init__(self, parent: RobotLanguageServerProtocol) -> None:
        super().__init__(parent)
        parent.semantic_tokens.token_types += [e for e in RobotSemTokenTypes]
        parent.semantic_tokens.token_modifiers += [e for e in RobotSemTokenModifiers]

        parent.semantic_tokens.collect_full.add(self.collect_full)
        # parent.semantic_tokens.collect_range.add(self.collect_range)
        # parent.semantic_tokens.collect_full_delta.add(self.collect_full_delta)

    @classmethod
    def generate_mapping(cls) -> Dict[str, Tuple[Enum, Optional[Set[Enum]]]]:
        from robot.parsing.lexer.tokens import Token as RobotToken

        definition: Dict[FrozenSet[str], Tuple[Enum, Optional[Set[Enum]]]] = {
            frozenset(RobotToken.HEADER_TOKENS): (RobotSemTokenTypes.HEADER, None),
            frozenset({RobotToken.SETTING_HEADER}): (RobotSemTokenTypes.HEADER_SETTING, None),
            frozenset({RobotToken.VARIABLE_HEADER}): (RobotSemTokenTypes.HEADER_VARIABLE, None),
            frozenset({RobotToken.TESTCASE_HEADER}): (RobotSemTokenTypes.HEADER_TESTCASE, None),
            frozenset({RobotToken.KEYWORD_HEADER}): (RobotSemTokenTypes.HEADER_KEYWORD, None),
            frozenset({RobotToken.COMMENT_HEADER}): (RobotSemTokenTypes.HEADER_COMMENT, None),
            frozenset({RobotToken.COMMENT}): (SemanticTokenTypes.COMMENT, None),
            frozenset(RobotToken.SETTING_TOKENS): (RobotSemTokenTypes.SETTING, None),
            frozenset({RobotToken.LIBRARY, RobotToken.RESOURCE, RobotToken.VARIABLES, RobotToken.WITH_NAME}): (
                RobotSemTokenTypes.SETTING_IMPORT,
                None,
            ),
            frozenset({RobotToken.TESTCASE_NAME}): (
                RobotSemTokenTypes.TESTCASE_NAME,
                {SemanticTokenModifiers.DECLARATION},
            ),
            frozenset({RobotToken.KEYWORD_NAME}): (
                RobotSemTokenTypes.KEYWORD_NAME,
                {SemanticTokenModifiers.DECLARATION},
            ),
            frozenset(
                {
                    RobotToken.RETURN,
                    RobotToken.FOR,
                    RobotToken.FOR_SEPARATOR,
                    RobotToken.END,
                    RobotToken.IF,
                    RobotToken.ELSE_IF,
                    RobotToken.ELSE,
                }
            ): (RobotSemTokenTypes.CONTROL_FLOW, None),
            frozenset({RobotToken.FOR_SEPARATOR}): (RobotSemTokenTypes.FOR_SEPARATOR, None),
            frozenset({RobotToken.ARGUMENT}): (RobotSemTokenTypes.ARGUMENT, None),
            frozenset({RobotToken.VARIABLE, RobotToken.ASSIGN}): (RobotSemTokenTypes.VARIABLE, None),
            frozenset({RobotToken.KEYWORD}): (RobotSemTokenTypes.KEYWORD, None),
            frozenset({ROBOT_KEYWORD_INNER}): (RobotSemTokenTypes.KEYWORD_INNER, None),
            frozenset({RobotToken.NAME}): (RobotSemTokenTypes.NAME, None),
            frozenset({RobotToken.CONTINUATION}): (RobotSemTokenTypes.CONTINUATION, None),
            frozenset({RobotToken.SEPARATOR}): (RobotSemTokenTypes.SEPARATOR, None),
            frozenset({RobotToken.EOL, RobotToken.EOS}): (RobotSemTokenTypes.TERMINATOR, None),
        }

        result: Dict[str, Tuple[Enum, Optional[Set[Enum]]]] = {}
        for k, v in definition.items():
            for e in k:
                result[e] = v

        return result

    __mapping: Optional[Dict[str, Tuple[Enum, Optional[Set[Enum]]]]] = None

    @classmethod
    def mapping(cls) -> Dict[str, Tuple[Enum, Optional[Set[Enum]]]]:
        if cls.__mapping is None:
            cls.__mapping = cls.generate_mapping()
        return cls.__mapping

    ESCAPE_REGEX = re.compile(
        r"(?P<t>[^\\]+)|(?P<x>\\([^xuU]|x[0-f]{2}|u[0-f]{4}|U[0-f]{8}){0,1})", re.MULTILINE | re.DOTALL
    )

    BUILTIN_MATCHER = KeywordMatcher("BuiltIn")

    @classmethod
    async def generate_sem_sub_tokens(
        cls,
        namespace: Namespace,
        builtin_library_doc: Optional[LibraryDoc],
        token: Token,
        node: ast.AST,
        col_offset: Optional[int] = None,
        length: Optional[int] = None,
    ) -> AsyncGenerator[SemTokenInfo, None]:
        from robot.parsing.lexer.tokens import Token as RobotToken
        from robot.parsing.model.statements import (
            Documentation,
            Fixture,
            LibraryImport,
            Metadata,
            ResourceImport,
            VariablesImport,
        )
        from robot.variables.search import is_variable

        sem_info = cls.mapping().get(token.type, None) if token.type is not None else None
        if sem_info is not None:
            sem_type, sem_mod = sem_info

            if isinstance(node, (Documentation, Metadata)):
                sem_mod = {SemanticTokenModifiers.DOCUMENTATION}

            if token.type == RobotToken.VARIABLE:
                if is_variable(token.value):
                    if col_offset is None:
                        col_offset = token.col_offset
                    if length is None:
                        length = token.end_col_offset - token.col_offset

                    yield SemTokenInfo(token.lineno, col_offset, 2, RobotSemTokenTypes.VARIABLE_BEGIN, sem_mod)
                    yield SemTokenInfo.from_token(token, sem_type, sem_mod, col_offset + 2, length - 3)
                    yield SemTokenInfo(
                        token.lineno, col_offset + length - 1, 1, RobotSemTokenTypes.VARIABLE_END, sem_mod
                    )
                else:
                    yield SemTokenInfo.from_token(token, sem_type, sem_mod)

            elif token.type == RobotToken.ARGUMENT and "\\" in token.value:
                if col_offset is None:
                    col_offset = token.col_offset
                if length is None:
                    length = token.end_col_offset - token.col_offset

                for g in cls.ESCAPE_REGEX.finditer(token.value):
                    yield SemTokenInfo.from_token(
                        token,
                        sem_info[0] if g.group("x") is None or g.end() - g.start() == 1 else RobotSemTokenTypes.ESCAPE,
                        sem_info[1],
                        col_offset + g.start(),
                        g.end() - g.start(),
                    )
            elif token.type == RobotToken.KEYWORD or (token.type == RobotToken.NAME and isinstance(node, Fixture)):
                if col_offset is None:
                    col_offset = token.col_offset
                if length is None:
                    length = token.end_col_offset - token.col_offset

                kw_namespace: Optional[str] = None
                kw: str = token.value

                for lib, name in yield_owner_and_kw_names(token.value):
                    if lib is not None:
                        lib_matcher = KeywordMatcher(lib)
                        if (
                            lib_matcher in (await namespace.get_libraries_matchers()).keys()
                            or lib_matcher in (await namespace.get_resources_matchers()).keys()
                        ):
                            kw_namespace = lib
                            if name:
                                kw = name
                            break

                kw_index = token.value.index(kw)

                if kw_namespace:
                    yield SemTokenInfo(
                        token.lineno,
                        col_offset,
                        len(kw_namespace),
                        RobotSemTokenTypes.NAMESPACE,
                        {RobotSemTokenModifiers.BUILTIN} if kw_namespace == cls.BUILTIN_MATCHER else None,
                    )
                    yield SemTokenInfo(
                        token.lineno,
                        col_offset + len(kw_namespace),
                        1,
                        SemanticTokenTypes.OPERATOR,
                    )
                if builtin_library_doc is not None and kw in builtin_library_doc.keywords:
                    doc = await namespace.find_keyword(token.value)
                    if (
                        doc is not None
                        and doc.libname == cls.BUILTIN_MATCHER
                        and KeywordMatcher(doc.name) == KeywordMatcher(kw)
                    ):
                        if not sem_mod:
                            sem_mod = set()
                        sem_mod.add(RobotSemTokenModifiers.BUILTIN)

                yield SemTokenInfo.from_token(token, sem_type, sem_mod, col_offset + kw_index, len(kw))
            elif token.type == RobotToken.NAME and isinstance(node, (LibraryImport, ResourceImport, VariablesImport)):
                yield SemTokenInfo.from_token(token, RobotSemTokenTypes.NAMESPACE, sem_mod, col_offset, length)
            else:
                yield SemTokenInfo.from_token(token, sem_type, sem_mod, col_offset, length)

    async def generate_sem_tokens(
        self,
        token: Token,
        node: ast.AST,
        namespace: Namespace,
        builtin_library_doc: Optional[LibraryDoc],
    ) -> AsyncGenerator[SemTokenInfo, None]:
        from robot.parsing.lexer.tokens import Token as RobotToken

        if token.type in {*RobotToken.ALLOW_VARIABLES, RobotToken.KEYWORD}:

            for sub_token in tokenize_variables(
                token, ignore_errors=True, identifiers="$" if token.type == RobotToken.KEYWORD_NAME else "$@&%"
            ):
                async for e in self.generate_sem_sub_tokens(namespace, builtin_library_doc, sub_token, node):
                    yield e

        else:
            async for e in self.generate_sem_sub_tokens(namespace, builtin_library_doc, token, node):
                yield e

    async def generate_run_kw_tokens(
        self,
        namespace: Namespace,
        builtin_library_doc: Optional[LibraryDoc],
        kw_doc: Optional[KeywordDoc],
        kw_token: Token,
        arguments: List[Token],
        node: ast.AST,
    ) -> AsyncGenerator[Tuple[Token, ast.AST], None]:
        from robot.parsing.lexer import Token as RobotToken
        from robot.utils.escaping import unescape

        async def skip_non_data_tokens() -> AsyncGenerator[Tuple[Token, ast.AST], None]:
            nonlocal arguments
            while arguments and arguments[0] and arguments[0].type in RobotToken.NON_DATA_TOKENS:

                yield arguments[0], node,
                arguments = arguments[1:]

        yield kw_token, node

        if kw_doc is not None and kw_doc.is_any_run_keyword():
            async for b in skip_non_data_tokens():
                yield b

            if kw_doc.is_run_keyword() and len(arguments) > 0:
                token = arguments[0]
                async for b in self.generate_run_kw_tokens(
                    namespace,
                    builtin_library_doc,
                    await namespace.find_keyword(unescape(token.value)) if is_not_variable_token(token) else None,
                    RobotToken(ROBOT_KEYWORD_INNER, token.value, token.lineno, token.col_offset, token.error),
                    arguments[1:],
                    node,
                ):
                    yield b
            elif kw_doc.is_run_keyword_with_condition() and len(arguments) > 0:
                yield arguments[0], node,
                arguments = arguments[1:]

                async for b in skip_non_data_tokens():
                    yield b

                if len(arguments) > 0:
                    token = arguments[0]
                    async for b in self.generate_run_kw_tokens(
                        namespace,
                        builtin_library_doc,
                        await namespace.find_keyword(unescape(token.value)) if is_not_variable_token(token) else None,
                        RobotToken(ROBOT_KEYWORD_INNER, token.value, token.lineno, token.col_offset, token.error),
                        arguments[1:],
                        node,
                    ):
                        yield b
            elif kw_doc.is_run_keywords() and len(arguments) > 0:
                has_separator = False
                while arguments:
                    async for b in skip_non_data_tokens():
                        yield b

                    if not arguments:
                        break

                    token = arguments[0]
                    arguments = arguments[1:]

                    if token.value == "AND":
                        yield RobotToken(
                            RobotToken.ELSE, token.value, token.lineno, token.col_offset, token.error
                        ), node
                        continue

                    separator_token = next((e for e in arguments if e.value == "AND"), None)
                    args = []
                    if separator_token is not None:
                        args = arguments[: arguments.index(separator_token)]
                        arguments = arguments[arguments.index(separator_token) :]
                        has_separator = True
                    else:
                        if has_separator:
                            args = arguments
                            arguments = []

                    async for e in self.generate_run_kw_tokens(
                        namespace,
                        builtin_library_doc,
                        await namespace.find_keyword(unescape(token.value)) if is_not_variable_token(token) else None,
                        RobotToken(ROBOT_KEYWORD_INNER, token.value, token.lineno, token.col_offset, token.error),
                        args,
                        node,
                    ):
                        yield e
            elif kw_doc.is_run_keyword_if() and len(arguments) > 0:

                async def generate_run_kw_if() -> AsyncGenerator[Tuple[Token, ast.AST], None]:
                    nonlocal arguments

                    yield arguments[0], node,
                    arguments = arguments[1:]
                    has_separator = False
                    while arguments:
                        async for b in skip_non_data_tokens():
                            yield b

                        if not arguments:
                            break

                        token = arguments[0]
                        arguments = arguments[1:]

                        if token.value in ["ELSE", "ELSE IF"]:
                            yield RobotToken(
                                RobotToken.ELSE, token.value, token.lineno, token.col_offset, token.error
                            ), node

                            if token.value == "ELSE IF":
                                async for b in skip_non_data_tokens():
                                    yield b

                                if not arguments:
                                    break

                                yield arguments[0], node
                                arguments = arguments[1:]
                            continue

                        inner_kw_doc = (
                            await namespace.find_keyword(unescape(token.value))
                            if is_not_variable_token(token)
                            else None
                        )

                        if inner_kw_doc is not None and inner_kw_doc.is_run_keyword_if():
                            yield RobotToken(
                                ROBOT_KEYWORD_INNER, token.value, token.lineno, token.col_offset, token.error
                            ), node

                            arguments = arguments[1:]

                            async for b in skip_non_data_tokens():
                                yield b

                            async for e in generate_run_kw_if():
                                yield e

                            continue

                        separator_token = next((e for e in arguments if e.value in ["ELSE", "ELSE IF"]), None)
                        args = []

                        if separator_token is not None:
                            args = arguments[: arguments.index(separator_token)]
                            arguments = arguments[arguments.index(separator_token) :]
                            has_separator = True
                        else:
                            if has_separator:
                                args = arguments
                                arguments = []

                        async for e in self.generate_run_kw_tokens(
                            namespace,
                            builtin_library_doc,
                            inner_kw_doc,
                            RobotToken(ROBOT_KEYWORD_INNER, token.value, token.lineno, token.col_offset, token.error),
                            args,
                            node,
                        ):
                            yield e

                async for e in generate_run_kw_if():
                    yield e
        else:

            for a in arguments:
                yield a, node

    async def collect(
        self,
        model: ast.AST,
        range: Optional[Range],
        namespace: Namespace,
        builtin_library_doc: Optional[LibraryDoc],
        cancel_token: CancelationToken,
    ) -> Union[SemanticTokens, SemanticTokensPartialResult, None]:

        from robot.parsing.lexer.tokens import Token as RobotToken
        from robot.parsing.model.statements import Fixture, KeywordCall

        data = []
        last_line = 0
        last_col = 0

        async def get_tokens() -> AsyncGenerator[Tuple[Token, ast.AST], None]:
            for node in iter_nodes(model):
                if isinstance(node, HasTokens):
                    if isinstance(node, (KeywordCall, Fixture)):
                        kw_token = cast(
                            Token,
                            node.get_token(RobotToken.KEYWORD)
                            if isinstance(node, KeywordCall)
                            else node.get_token(RobotToken.NAME),
                        )
                        kw: Optional[str] = None

                        for _, name in yield_owner_and_kw_names(kw_token.value):
                            if name is not None:
                                matcher = KeywordMatcher(name)
                                if matcher in ALL_RUN_KEYWORDS_MATCHERS:
                                    kw = name
                        if kw:
                            kw_doc = await namespace.find_keyword(kw_token.value)
                            if kw_doc is not None and kw_doc.is_any_run_keyword():
                                async for t in self.generate_run_kw_tokens(
                                    namespace,
                                    builtin_library_doc,
                                    kw_doc,
                                    kw_token,
                                    node.tokens[node.tokens.index(kw_token) + 1 :],
                                    node,
                                ):
                                    yield t
                                continue

                    for token in node.tokens:
                        yield token, node

        async for robot_token, robot_node in async_takewhile(
            lambda t: not cancel_token.throw_if_canceled() and (range is None or token_in_range(t[0], range)),
            async_dropwhile(
                lambda t: not cancel_token.throw_if_canceled()
                and range is not None
                and not token_in_range(t[0], range),
                get_tokens(),
            ),
        ):
            cancel_token.throw_if_canceled()

            async for token in self.generate_sem_tokens(robot_token, robot_node, namespace, builtin_library_doc):
                current_line = token.lineno - 1

                data.append(current_line - last_line)

                if last_line != current_line:
                    last_col = token.col_offset
                    data.append(last_col)
                else:
                    delta = token.col_offset - last_col
                    data.append(delta)
                    last_col += delta

                last_line = current_line

                data.append(token.length)

                data.append(self.parent.semantic_tokens.token_types.index(token.sem_token_type))

                data.append(
                    reduce(
                        operator.or_,
                        (2 ** self.parent.semantic_tokens.token_modifiers.index(e) for e in token.sem_modifiers),
                    )
                    if token.sem_modifiers
                    else 0
                )

        return SemanticTokens(data=data)

    async def collect_threading(
        self, document: TextDocument, range: Optional[Range]
    ) -> Union[SemanticTokens, SemanticTokensPartialResult, None]:
        cancel_token = CancelationToken()
        try:
            model = await self.parent.documents_cache.get_model(document)
            namespace = await self.parent.documents_cache.get_namespace(document)

            builtin_library_doc = next(
                (
                    library.library_doc
                    for library in (await namespace.get_libraries()).values()
                    if library.name == BUILTIN_LIBRARY_NAME
                    and library.import_name == BUILTIN_LIBRARY_NAME
                    and library.import_range == Range.zero()
                ),
                None,
            )
            await namespace.get_library_doc()

            return await asyncio.get_running_loop().run_in_executor(
                None,
                asyncio.run,
                self.collect(
                    model,
                    range,
                    namespace,
                    builtin_library_doc,
                    cancel_token,
                ),
            )
        except BaseException:
            cancel_token.cancel()
            raise

    @language_id("robotframework")
    async def collect_full(
        self, sender: Any, document: TextDocument, **kwargs: Any
    ) -> Union[SemanticTokens, SemanticTokensPartialResult, None]:
        return await document.get_cache(self.collect_threading, None)

    @language_id("robotframework")
    async def collect_range(
        self, sender: Any, document: TextDocument, range: Range, **kwargs: Any
    ) -> Union[SemanticTokens, SemanticTokensPartialResult, None]:
        return await self.collect_threading(document, range)

    @language_id("robotframework")
    async def collect_full_delta(
        self, sender: Any, document: TextDocument, previous_result_id: str, **kwargs: Any
    ) -> Union[SemanticTokens, SemanticTokensDelta, SemanticTokensDeltaPartialResult, None]:
        return None
