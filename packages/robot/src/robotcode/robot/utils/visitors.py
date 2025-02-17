import ast
from abc import ABC
from collections import defaultdict

from typing_extensions import Any, AsyncIterator, Callable, Dict, Iterator, Optional, Type, Union

from robot.parsing.model.statements import Statement

__all__ = ["iter_fields", "iter_child_nodes", "AsyncVisitor"]


def _patch_robot() -> None:
    if hasattr(Statement, "_fields"):
        Statement._fields = ()


_patch_robot()


def iter_fields(node: ast.AST) -> Iterator[Any]:
    for field in node._fields:
        try:
            yield field, getattr(node, field)
        except AttributeError:
            pass


def iter_field_values(node: ast.AST) -> Iterator[Any]:
    for field in node._fields:
        try:
            yield getattr(node, field)
        except AttributeError:
            pass


def iter_child_nodes(node: ast.AST) -> Iterator[ast.AST]:
    for _name, field in iter_fields(node):
        if isinstance(field, ast.AST):
            yield field
        elif isinstance(field, list):
            for item in field:
                if isinstance(item, ast.AST):
                    yield item


async def iter_nodes(node: ast.AST) -> AsyncIterator[ast.AST]:
    for _name, value in iter_fields(node):
        if isinstance(value, list):
            for item in value:
                if isinstance(item, ast.AST):
                    yield item
                    async for n in iter_nodes(item):
                        yield n

        elif isinstance(value, ast.AST):
            yield value

            async for n in iter_nodes(value):
                yield n


class _NotSet:
    pass


class VisitorFinder(ABC):
    __NOT_SET = _NotSet()
    __cls_finder_cache__: Dict[Type[Any], Union[Callable[..., Any], None, _NotSet]]

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        cls.__cls_finder_cache__ = defaultdict(lambda: cls.__NOT_SET)

    @classmethod
    def __find_visitor(cls, node_cls: Type[Any]) -> Optional[Callable[..., Any]]:
        if node_cls is ast.AST:
            return None
        method_name = "visit_" + node_cls.__name__
        method = getattr(cls, method_name, None)
        if callable(method):
            return method  # type: ignore[no-any-return]
        for base in node_cls.__bases__:
            method = cls._find_visitor(base)
            if method:
                return method
        return None

    @classmethod
    def _find_visitor(cls, node_cls: Type[Any]) -> Optional[Callable[..., Any]]:
        result = cls.__cls_finder_cache__[node_cls]
        if result is cls.__NOT_SET:
            result = cls.__cls_finder_cache__[node_cls] = cls.__find_visitor(node_cls)
        return result  # type: ignore[return-value]


class AsyncVisitor(VisitorFinder):
    async def visit(self, node: ast.AST) -> None:
        visitor = self._find_visitor(type(node)) or self.__class__.generic_visit
        await visitor(self, node)

    async def generic_visit(self, node: ast.AST) -> None:
        for value in iter_field_values(node):
            if value is None:
                continue
            if isinstance(value, ast.AST):
                await self.visit(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        await self.visit(item)


class Visitor(VisitorFinder):
    def visit(self, node: ast.AST) -> None:
        visitor = self._find_visitor(type(node)) or self.__class__.generic_visit
        visitor(self, node)

    def generic_visit(self, node: ast.AST) -> None:
        for value in iter_field_values(node):
            if value is None:
                continue
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, ast.AST):
                        self.visit(item)
            else:
                self.visit(value)
