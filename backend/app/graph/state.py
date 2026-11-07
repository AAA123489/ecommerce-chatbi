"""LangGraph 的状态定义。

图里每个节点都读这个字典、返回它的**部分**字段，LangGraph 负责合并。
``trace`` 带 reducer，每次追加而不是覆盖，方便前端做流式进度。
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


def event(node: str, message: str) -> dict[str, Any]:
    """往 trace 里追加一条事件。"""
    return {"node": node, "message": message}


class AnalysisState(TypedDict, total=False):
    """一次问答的完整状态。"""

    # 输入
    question: str

    # 意图识别
    metric_key: str | None

    # 生成与修复
    sql: str | None
    attempts: int           # 已经生成过几次 SQL
    failure: str | None     # 上一次失败的原因，原样回填给模型
    failures: Annotated[list[dict[str, Any]], operator.add]
    #: 每次失败追加一条 {"kind": guard_fail | exec_error | empty_result, "detail": ...}
    #: 评测按 kind 分桶算修复成功率，所以这里记全量，不是只记最后一次
    empty_retried: bool     # 空结果是不是已经改写过一次了

    # 执行结果
    columns: list[str]
    rows: list[dict[str, Any]]

    # 输出
    answer: str
    chart: dict[str, Any] | None   # 前端拿这个决定画什么
    status: str            # running | ok | clarify | failed

    # 进度事件流
    trace: Annotated[list[dict[str, Any]], operator.add]
