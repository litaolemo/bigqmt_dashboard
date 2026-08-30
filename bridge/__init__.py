"""大QMT 直连层。

改造前 dashboard 是被动方：QMT 端脚本 push 数据上来，顺手把服务端排好的指令队列
取走自己执行。现在反过来——dashboard 通过 xtquant_big_convert 的 RPC 直接查账户、
直接下单，指令队列整个消失。

分工：
    config  账号 → 连接参数（每账号一套，支持连不同机器的大QMT）
    pool    account_id → BigQmtXtTrader / BigQmtXtData 实例缓存与探活
    orders  唯一下单出口，服务端风控闸门在这里
    market  行情读取，全局共用一条连接
"""
