"""持久化：全 JSON，无任何二进制数据库。

进行中追加 ``events.jsonl``，对局结束时归档 ``events.json`` 并写 ``meta.json``，
另外维护一份 ``index.json`` 供历史列表使用。
"""
