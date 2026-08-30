"""账户数据同步。

改造前这层不存在：QMT 客户端 push 什么，服务端存什么。现在 dashboard 主动去大QMT
取（poller），成交回报则由大QMT 经 Redis 实时推过来（callbacks）。
"""
