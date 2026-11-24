# 数据与模块契约

## 路径

`data/games/{appid}_{名称}/info.json`保存游戏资料；header图片和cover.json保存封面及校验；snapshots.jsonl保存价格和评分观察历史；reviews/YYYY-MM/part_NNNNN.jsonl保存评测。

月份依据created_at的北京时间。data/tasks/{run_id}只保存manifest、checkpoint、errors、summary；提交中断时额外有pending_page。

## 评测

UTF-8 JSONL，每行恰好28个键，顺序由contracts.REVIEW_FIELD_KEYS规定。未知为null；ID为字符串，时间为UTC Unix秒，时长为分钟，内存与显存为MB。正文保留原文、表情和换行。

SQLite reviews按review_id唯一，review_locations记录原分片。新任务更新原文件，较早collected_at不能覆盖较新内容。容量按该游戏所有月份实际JSONL字节累计，已有评测更新按空间净增量检查。封面、数据库、日志不计入。

## 提交协议

prepare_page只生成写入计划。额度通过后commit_page开启事务，保存带文件内容、评测与检查点的日志，再原子替换文件、更新数据库与检查点；成功后删除日志。

文件写完而数据库未提交时，恢复优先幂等重放日志，不能将已有文件当作另一页新增空间。失败不删除恢复日志，不伪造成功。操作系统文件锁防止并发采集改写。

## 模块边界

crawlers负责网络；processing负责映射与校验；storage负责唯一数据、事务、文件恢复与锁；services负责容量与编排；scripts只接参数。import不联网、不启动采集。未来工具复用结构化服务函数，不解析print，不开放任意SQL和路径。

## 参数与状态

任务ID只能包含字母、数字、下划线、连字符。恢复验证固定窗口、游戏清单与请求参数，使用冻结策略。覆盖优先级：显式单游戏 > 显式全局 > 配置单游戏 > 配置全局 > 模块默认；显式改额度记录历史。

completed表示所请求月份全部完成，partial/capacity_limit表示容量停止，paused表示人工或环境暂停，failed表示错误。实际覆盖日期从评测记录计算，不用整月边界代替。
