"""LLM Agent 运行时。

这里是项目里唯一认识「模型」的地方。它不认识规则引擎，
只把 :class:`LegalAction` 翻译成工具、把模型的工具调用翻译回 :class:`Action`。
编排层是唯一同时认识它和引擎的层（AGENTS.md §5 原则 4）。
"""
