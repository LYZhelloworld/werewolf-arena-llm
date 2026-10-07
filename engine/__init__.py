"""规则引擎包：纯确定性，无 I/O、无 LLM 依赖。

刻意不在 ``__init__`` 里导出子模块，避免 ``engine.engine`` 与 ``engine.phases``
之间的循环导入；请按需显式导入，例如 ``from engine.engine import RuleEngine``。
"""
