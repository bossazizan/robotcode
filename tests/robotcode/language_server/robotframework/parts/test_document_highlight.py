import asyncio
from pathlib import Path

import pytest
import yaml
from robotcode.core.lsp.types import Position
from robotcode.language_server.common.text_document import TextDocument
from robotcode.language_server.robotframework.protocol import (
    RobotLanguageServerProtocol,
)

from tests.robotcode.language_server.robotframework.tools import (
    GeneratedTestData,
    generate_test_id,
    generate_tests_from_source_document,
)

from .pytest_regtestex import RegTestFixtureEx


@pytest.mark.parametrize(
    ("test_document", "data"),
    generate_tests_from_source_document(Path(Path(__file__).parent, "data/tests/document_highlight.robot")),
    indirect=["test_document"],
    ids=generate_test_id,
    scope="package",
)
@pytest.mark.usefixtures("protocol")
@pytest.mark.asyncio()
async def test(
    regtest: RegTestFixtureEx,
    protocol: RobotLanguageServerProtocol,
    test_document: TextDocument,
    data: GeneratedTestData,
) -> None:
    result = await asyncio.wait_for(
        protocol.robot_document_highlight.collect(
            protocol.robot_document_highlight,
            test_document,
            Position(line=data.line, character=data.character),
        ),
        60,
    )
    regtest.write(
        yaml.dump(
            {
                "data": data,
                "result": sorted(result, key=lambda v: (v.range.start, v.range.end, v.kind)) if result else result,
            }
        )
    )
