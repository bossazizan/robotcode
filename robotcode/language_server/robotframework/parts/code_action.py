from __future__ import annotations

import asyncio
import contextlib
import socket
import threading
import traceback
import urllib.parse
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Union, cast
from urllib.parse import parse_qs, urlparse

from ....jsonrpc2.protocol import rpc_method
from ....utils.logging import LoggingDescriptor
from ....utils.net import find_free_port
from ....utils.uri import Uri
from ...common.decorators import (
    code_action_kinds,
    command,
    get_command_name,
    language_id,
)
from ...common.lsp_types import (
    AnnotatedTextEdit,
    ChangeAnnotation,
    CodeAction,
    CodeActionContext,
    CodeActionKinds,
    CodeActionTriggerKind,
    Command,
    CreateFile,
    DeleteFile,
    MessageType,
    Model,
    OptionalVersionedTextDocumentIdentifier,
    Range,
    RenameFile,
    TextDocumentEdit,
    WorkspaceEdit,
)
from ...common.text_document import TextDocument
from ..configuration import DocumentationServerConfig
from ..diagnostics.library_doc import (
    get_library_doc,
    get_robot_library_html_doc_str,
    resolve_robot_variables,
)
from ..diagnostics.namespace import LibraryEntry, Namespace
from ..utils.ast_utils import Token, get_node_at_position, range_from_token
from ..utils.version import get_robot_version
from .model_helper import ModelHelperMixin

if TYPE_CHECKING:
    from ..protocol import RobotLanguageServerProtocol  # pragma: no cover

from string import Template

from .protocol_part import RobotLanguageServerProtocolPart


@dataclass(repr=False)
class ConvertUriParams(Model):
    uri: str


HTML_ERROR_TEMPLATE = Template(
    """\n
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>${type}: ${message}</title>
</head>
<body>
  <div id="content">
    <h3>
        ${type}: ${message}
    </h3>
    <pre>
${stacktrace}
    </pre>
  </div>

</body>
</html>
"""
)

MARKDOWN_TEMPLATE = Template(
    """\
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>${name}</title>
</head>
<body>
  <template type="markdown" id="markdown-content">${content}</template>
  <div id="content"></div>
  <script src="https://cdn.jsdelivr.net/npm/marked/marked.min.js"></script>
  <script>
    document.getElementById('content').innerHTML =
      marked.parse(document.getElementById('markdown-content').content.textContent, {gfm: true});
  </script>
</body>
</html>
"""
)


class LibDocRequestHandler(SimpleHTTPRequestHandler):
    _logger = LoggingDescriptor()

    def log_message(self, format: str, *args: Any) -> None:
        self._logger.info("%s - %s\n" % (self.address_string(), format % args))

    def log_error(self, format: str, *args: Any) -> None:
        self._logger.error("%s - %s\n" % (self.address_string(), format % args))

    def do_GET(self) -> None:  # noqa: N802

        query = parse_qs(urlparse(self.path).query)
        name = n[0] if (n := query.get("name", [])) else None
        args = n[0] if (n := query.get("args", [])) else None
        basedir = n[0] if (n := query.get("basedir", [])) else None
        type_ = n[0] if (n := query.get("type", [])) else None

        if name:
            try:
                if type_ in ["md", "markdown"]:
                    libdoc = get_library_doc(
                        name, tuple(args.split("::") if args else ()), base_dir=basedir if basedir else "."
                    )

                    def calc_md() -> str:
                        tt = str.maketrans({"<": "&lt;", ">": "&gt;"})
                        return libdoc.to_markdown(add_signature=False, only_doc=False, header_level=0).translate(tt)

                    data = MARKDOWN_TEMPLATE.substitute(content=calc_md(), name=name)

                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()

                    self.wfile.write(bytes(data, "utf-8"))
                else:
                    with ProcessPoolExecutor(max_workers=1) as executor:
                        result = executor.submit(
                            get_robot_library_html_doc_str,
                            name + ("::" + args if args else ""),
                            base_dir=basedir if basedir else ".",
                        ).result(10)

                        self.send_response(200)
                        self.send_header("Content-type", "text/html")
                        self.end_headers()

                        self.wfile.write(bytes(result, "utf-8"))
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as e:
                self.send_response(404)
                self.send_header("Content-type", "text/html")
                self.end_headers()

                self.wfile.write(
                    bytes(
                        HTML_ERROR_TEMPLATE.substitute(
                            type=type(e).__qualname__, message=str(e), stacktrace="".join(traceback.format_exc())
                        ),
                        "utf-8",
                    )
                )

        else:
            super().do_GET()


class DualStackServer(ThreadingHTTPServer):
    def server_bind(self) -> None:
        # suppress exception when protocol is IPv4
        with contextlib.suppress(Exception):
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        return super().server_bind()


class RobotCodeActionProtocolPart(RobotLanguageServerProtocolPart, ModelHelperMixin):
    _logger = LoggingDescriptor()

    def __init__(self, parent: RobotLanguageServerProtocol) -> None:
        super().__init__(parent)

        parent.code_action.collect.add(self.collect)
        self.parent.on_initialized.add(self.initialized)
        self.parent.on_shutdown.add(self.shutdown)

        self._documentation_server: Optional[ThreadingHTTPServer] = None
        self._documentation_server_lock = threading.RLock()
        self._documentation_server_port = 0

        self.parent.commands.register(self.translate_suite)
        self.parent.commands.register(self.comming_soon)

    async def initialized(self, sender: Any) -> None:
        await self._ensure_http_server_started()

    async def shutdown(self, sender: Any) -> None:
        with self._documentation_server_lock:
            if self._documentation_server is not None:
                self._documentation_server.shutdown()
                self._documentation_server = None

    def _run_server(self, start_port: int, end_port: int) -> None:

        self._documentation_server_port = find_free_port(start_port, end_port)

        self._logger.info(f"Start documentation server on port {self._documentation_server_port}")

        with DualStackServer(("127.0.0.1", self._documentation_server_port), LibDocRequestHandler) as server:
            self._documentation_server = server
            try:
                server.serve_forever()
            except BaseException:
                self._documentation_server = None
                raise

    async def _ensure_http_server_started(self) -> None:
        config = await self.parent.workspace.get_configuration(DocumentationServerConfig)

        with self._documentation_server_lock:
            if self._documentation_server is None:
                self._server_thread = Thread(
                    name="documentation_server",
                    target=self._run_server,
                    args=(config.start_port, config.end_port),
                    daemon=True,
                )
                self._server_thread.start()

    @language_id("robotframework")
    @code_action_kinds(
        [
            f"{CodeActionKinds.SOURCE}.openDocumentation",
            f"{CodeActionKinds.QUICKFIX}.createKeyword",
        ]
    )
    @_logger.call
    async def collect(
        self, sender: Any, document: TextDocument, range: Range, context: CodeActionContext
    ) -> Optional[List[Union[Command, CodeAction]]]:

        from robot.parsing.lexer import Token as RobotToken
        from robot.parsing.model.statements import (
            Fixture,
            KeywordCall,
            LibraryImport,
            ResourceImport,
        )

        namespace = await self.parent.documents_cache.get_namespace(document)
        if namespace is None:
            return None

        model = await self.parent.documents_cache.get_model(document, False)
        node = await get_node_at_position(model, range.start)

        if get_robot_version() >= (5, 1):
            from robot.conf.languages import En, Languages
            from robot.parsing.model.statements import Config

            if context.only and CodeActionKinds.SOURCE in context.only and isinstance(node, Config):
                for token in node.get_tokens(RobotToken.CONFIG):
                    config, lang = token.value.split(":", 1)

                    if config.lower() == "language" and lang and range.start in range_from_token(token):
                        try:
                            languages = Languages(lang)
                            language = next((v for v in languages.languages if v is not En), None)
                        except (SystemExit, KeyboardInterrupt, asyncio.CancelledError):
                            raise
                        except BaseException:
                            language = None

                        if language is not None:

                            return [
                                CodeAction(
                                    f"Translate file to `{language.name}`",
                                    kind=CodeActionKinds.SOURCE + ".openDocumentation",
                                    command=Command(
                                        f"Translate Suite to {lang}",
                                        get_command_name(self.translate_suite),
                                        [document.document_uri, lang],
                                    ),
                                )
                            ]
                        else:
                            return None

        if context.only and isinstance(node, (LibraryImport, ResourceImport)):

            if CodeActionKinds.SOURCE in context.only:
                url = await self.build_url(
                    node.name, node.args if isinstance(node, LibraryImport) else (), document, namespace
                )

                return [
                    CodeAction(
                        "Open Documentation",
                        kind=CodeActionKinds.SOURCE + ".openDocumentation",
                        command=Command(
                            "Open Documentation",
                            "robotcode.showDocumentation",
                            [url],
                        ),
                    )
                ]

        if isinstance(node, (KeywordCall, Fixture)):
            result = await self.get_keyworddoc_and_token_from_position(
                node.keyword if isinstance(node, KeywordCall) else node.name,
                cast(Token, node.get_token(RobotToken.KEYWORD if isinstance(node, KeywordCall) else RobotToken.NAME)),
                [cast(Token, t) for t in node.get_tokens(RobotToken.ARGUMENT)],
                namespace,
                range.start,
            )

            if result is None and (
                (context.only and CodeActionKinds.QUICKFIX in context.only)
                or context.trigger_kind == CodeActionTriggerKind.AUTOMATIC
            ):
                return [
                    CodeAction(
                        "Create Keyword",
                        kind=CodeActionKinds.QUICKFIX + ".createKeyword",
                        command=Command(
                            "Create Keyword",
                            "robotcode.commingSoon",
                        ),
                    )
                ]

            if result is not None:
                kw_doc, _ = result

                if kw_doc is not None:

                    if context.only and CodeActionKinds.SOURCE in context.only:
                        entry: Optional[LibraryEntry] = None

                        if kw_doc.libtype == "LIBRARY":
                            entry = next(
                                (
                                    v
                                    for v in (await namespace.get_libraries()).values()
                                    if v.library_doc == kw_doc.parent
                                ),
                                None,
                            )

                        elif kw_doc.libtype == "RESOURCE":
                            entry = next(
                                (
                                    v
                                    for v in (await namespace.get_resources()).values()
                                    if v.library_doc == kw_doc.parent
                                ),
                                None,
                            )

                            self_libdoc = await namespace.get_library_doc()
                            if entry is None and self_libdoc == kw_doc.parent:

                                entry = LibraryEntry(self_libdoc.name, str(document.uri.to_path().name), self_libdoc)

                        if entry is None:
                            return None

                        url = await self.build_url(entry.import_name, entry.args, document, namespace, kw_doc.name)

                        return [
                            CodeAction(
                                "Open Documentation",
                                kind=CodeActionKinds.SOURCE + ".openDocumentation",
                                command=Command(
                                    "Open Documentation",
                                    "robotcode.showDocumentation",
                                    [url],
                                ),
                            )
                        ]

        return None

    async def build_url(
        self,
        name: str,
        args: Tuple[Any, ...],
        document: TextDocument,
        namespace: Namespace,
        target: Optional[str] = None,
    ) -> str:

        base_dir = str(document.uri.to_path().parent)

        robot_variables = resolve_robot_variables(
            str(namespace.imports_manager.folder.to_path()),
            base_dir,
            variables=await namespace.get_resolvable_variables(),
        )
        try:
            name = robot_variables.replace_string(name.replace("\\", "\\\\"), ignore_errors=False)

            args = tuple(robot_variables.replace_string(v.replace("\\", "\\\\"), ignore_errors=False) for v in args)

        except (SystemExit, KeyboardInterrupt):
            raise
        except BaseException:
            pass

        url_args = "::".join(args) if args else ""

        base_url = f"http://localhost:{self._documentation_server_port}"
        params = urllib.parse.urlencode({"name": name, "args": url_args, "basedir": base_dir})

        url = f"{base_url}" f"/?&{params}" f"{f'#{target}' if target else ''}"

        return url

    @rpc_method(name="robot/documentationServer/convertUri", param_type=ConvertUriParams)
    @_logger.call
    async def _convert_uri(self, uri: str, *args: Any, **kwargs: Any) -> Optional[str]:
        real_uri = Uri(uri)

        folder = self.parent.workspace.get_workspace_folder(real_uri)

        if folder:
            path = real_uri.to_path().relative_to(folder.uri.to_path())

            return f"http://localhost:{self._documentation_server_port}/{path.as_posix()}"

        return None

    @command("robotcode.commingSoon")
    async def comming_soon(self) -> None:
        self.parent.window.show_message("Comming soon... stay tuned ...")

    @command("robotcode.translateSuite")
    async def translate_suite(self, document_uri: str, lang: str) -> None:
        from robot.conf.languages import Language
        from robot.parsing.lexer.tokens import Token as RobotToken

        try:
            language = Language.from_name(lang)
        except ValueError:
            self.parent.window.show_message(f"Invalid language {lang}", MessageType.ERROR)
            return

        document = await self.parent.documents.get(document_uri)

        if document is None:
            return

        changes: List[Union[TextDocumentEdit, CreateFile, RenameFile, DeleteFile]] = []

        header_translations = {
            RobotToken.SETTING_HEADER: language.settings_header,
            RobotToken.VARIABLE_HEADER: language.variables_header,
            RobotToken.TESTCASE_HEADER: language.test_cases_header,
            RobotToken.TASK_HEADER: language.tasks_header,
            RobotToken.KEYWORD_HEADER: language.keywords_header,
            RobotToken.COMMENT_HEADER: language.comments_header,
        }
        settings_translations = {
            RobotToken.LIBRARY: language.library_setting,
            RobotToken.DOCUMENTATION: language.documentation_setting,
            RobotToken.SUITE_SETUP: language.suite_setup_setting,
            RobotToken.SUITE_TEARDOWN: language.suite_teardown_setting,
            RobotToken.METADATA: language.metadata_setting,
            RobotToken.KEYWORD_TAGS: language.keyword_tags_setting,
            RobotToken.LIBRARY: language.library_setting,
            RobotToken.RESOURCE: language.resource_setting,
            RobotToken.VARIABLES: language.variables_setting,
            RobotToken.SETUP: f"[{language.setup_setting}]",
            RobotToken.TEARDOWN: f"[{language.teardown_setting}]",
            RobotToken.TEMPLATE: f"[{language.template_setting}]",
            RobotToken.TIMEOUT: f"[{language.timeout_setting}]",
            RobotToken.TAGS: f"[{language.tags_setting}]",
            RobotToken.ARGUMENTS: f"[{language.arguments_setting}]",
        }

        for token in await self.parent.documents_cache.get_tokens(document):
            if token.type in header_translations.keys():
                changes.append(
                    TextDocumentEdit(
                        OptionalVersionedTextDocumentIdentifier(str(document.uri), document.version),
                        [
                            AnnotatedTextEdit(
                                range_from_token(token),
                                f"*** { header_translations[token.type]} ***",
                                annotation_id="translate_settings",
                            )
                        ],
                    )
                )
            elif token.type in settings_translations.keys():
                changes.append(
                    TextDocumentEdit(
                        OptionalVersionedTextDocumentIdentifier(str(document.uri), document.version),
                        [
                            AnnotatedTextEdit(
                                range_from_token(token),
                                settings_translations[token.type],
                                annotation_id="translate_settings",
                            )
                        ],
                    )
                )
            else:
                pass

        if not changes:
            return

        edit = WorkspaceEdit(
            document_changes=changes,
            change_annotations={"translate_settings": ChangeAnnotation("Translate Settings", False)},
        )

        await self.parent.workspace.apply_edit(edit, "Translate")
