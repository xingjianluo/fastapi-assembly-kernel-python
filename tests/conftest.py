"""测试公共设施：配置内核日志，使每个用例的“输入 + 判定依据”可追溯。

单测执行时通过 logging 直接打印：

* 用例构造的输入（来源内容、依赖图、并发参数）；
* 期望判定（应返回值 / 应抛异常类型）与实际结果；
* 内核自身的 DEBUG 判定日志（来源覆盖、字段判定、装配、初始化）。
"""

from __future__ import annotations

import logging

import pytest

# 让内核 DEBUG 日志在单测中可见（log_cli 会将其打印到控制台）。
logging.getLogger("assembly_kernel").setLevel(logging.DEBUG)


@pytest.fixture
def case_log():
    """记录并打印用例输入与判定依据的简单辅助器。"""

    class CaseLogger:
        def __init__(self) -> None:
            self.log = logging.getLogger("tests.case")

        def input(self, **payload: object) -> None:
            self.log.info("用例输入: %s", payload)

        def expect(self, description: str) -> None:
            self.log.info("期望判定: %s", description)

        def actual(self, description: str) -> None:
            self.log.info("实际判定: %s", description)

    return CaseLogger()
