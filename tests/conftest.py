"""测试公共辅助：统一在单测日志中打印输入与判定依据。"""

from __future__ import annotations

import logging
from typing import Any, Callable

import pytest

logger = logging.getLogger("tests")


@pytest.fixture
def case_log() -> Callable[[str, Any, str], None]:
    """记录测试输入与判定依据（配合 pytest log_cli 输出）。"""

    def log(title: str, inputs: Any, rationale: str) -> None:
        logger.info("用例[%s] 输入: %r | 判定依据: %s", title, inputs, rationale)

    return log
