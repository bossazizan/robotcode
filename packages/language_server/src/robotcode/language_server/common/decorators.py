from typing import Any, Callable, List, Protocol, TypeVar, Union, runtime_checkable

from robotcode.core.lsp.types import CodeActionKind

from .text_document import TextDocument

_F = TypeVar("_F", bound=Callable[..., Any])

LANGUAGE_ID_ATTR = "__language_id__"


def language_id(id: str, *ids: str) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        setattr(func, LANGUAGE_ID_ATTR, {id, *ids})
        return func

    return decorator


TRIGGER_CHARACTERS_ATTR = "__trigger_characters__"


def trigger_characters(characters: List[str]) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        setattr(func, TRIGGER_CHARACTERS_ATTR, characters)
        return func

    return decorator


@runtime_checkable
class HasTriggerCharacters(Protocol):
    __trigger_characters__: List[str]


RETRIGGER_CHARACTERS_ATTR = "__retrigger_characters__"


@runtime_checkable
class HasRetriggerCharacters(Protocol):
    __retrigger_characters__: str


def retrigger_characters(characters: List[str]) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        setattr(func, RETRIGGER_CHARACTERS_ATTR, characters)
        return func

    return decorator


ALL_COMMIT_CHARACTERS_ATTR = "__all_commit_characters__"


def all_commit_characters(characters: List[str]) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        setattr(func, ALL_COMMIT_CHARACTERS_ATTR, characters)
        return func

    return decorator


@runtime_checkable
class HasAllCommitCharacters(Protocol):
    __all_commit_characters__: List[str]


CODE_ACTION_KINDS_ATTR = "__code_action_kinds__"


def code_action_kinds(kinds: List[Union[CodeActionKind, str]]) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        setattr(func, CODE_ACTION_KINDS_ATTR, kinds)
        return func

    return decorator


@runtime_checkable
class HasCodeActionKinds(Protocol):
    __code_action_kinds__: List[str]


def language_id_filter(language_id_or_document: Union[str, TextDocument]) -> Callable[[Any], bool]:
    def filter(c: Any) -> bool:
        return not hasattr(c, LANGUAGE_ID_ATTR) or (
            (
                language_id_or_document.language_id
                if isinstance(language_id_or_document, TextDocument)
                else language_id_or_document
            )
            in getattr(c, LANGUAGE_ID_ATTR)
        )

    return filter


COMMAND_ID_ATTR = "__command_name__"


def command(id: str) -> Callable[[_F], _F]:
    def decorator(func: _F) -> _F:
        setattr(func, COMMAND_ID_ATTR, id)
        return func

    return decorator


def is_command(func: Callable[..., Any]) -> bool:
    return hasattr(func, COMMAND_ID_ATTR)


def get_command_id(func: Callable[..., Any]) -> str:
    if hasattr(func, COMMAND_ID_ATTR):
        return str(getattr(func, COMMAND_ID_ATTR))

    raise TypeError(f"{func} is not a command.")
