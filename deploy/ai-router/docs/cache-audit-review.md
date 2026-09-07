# 缓存审计 commit review 与修复

审查首版：`7471ba7ea5910c562286061eb3c12abe5af82fe1`。

修复基于另一任务随后提交的 `20475450e48ab2e6ad811da447ba7fc22be8f630` 集成。没有回退它的路由诊断、策略编辑、执行尝试默认值或入口正文不归档限制；根目录已有的注册表提示等未提交改动不纳入本次修复。

## 已确认并修复的问题

| 优先级 | 触发条件及影响 | 修复 |
|---|---|---|
| P1 | 网关缺少原生cache_n，Router估算输入1000、实际重算100时，页面可能显示90%净复用 | 净复用和动态尾部只由同次执行的完整原生输入计数计算；缺失时不以Router估算补足 |
| P1 | WorkBuddy工作区记忆正常重排，但可选tools为null时，审计len(None)抛异常 | 工具列表检查兼容null，保留null与列表的区别；观察逻辑不再因此中断请求 |
| P2 | 短回复被身份处理器暂存，只在EOF释放时，首个输出和正文计时遗漏 | finish输出与普通流输出一样记录计时 |
| P2 | 网关排队超时503或准备失败502，已有终态遥测但错误响应未声明能力，Router不采集 | 已注册执行的错误响应也带遥测能力标识；未经认证的请求不声明已注册遥测 |
| P2 | 统计查询在途时提交新筛选或翻页，旧响应可能显示在新条件下，新查询被忽略 | 使旧结果失效并在当前查询结束后执行最新选择；旧错误也不覆盖新状态 |
| P2 | 不同工具或工作区记忆含相同文字，按全局子串计数误报重复搬移 | 以命名工具块、工作区记忆块分别检查次数 |
| P2 | 被重排用户消息的role/name丢失，或工作区占位区插入异常文本，规则可能误报通过 | 只允许指定内容字段改变，保留消息其余字段，并核对完整稳定占位内容 |

前三类测量/网关问题由独立子代理复现，父代理检查修复并验收；内容和页面问题由父代理使用合成输入及可控制请求完成顺序的测试复现。没有执行模型推理或重放旧对话。

## 验证

- 合并后的原有审计CPU契约：77项通过。
- 新增review回归：7项内容检查、2项计时/计数、3项网关错误遥测通过。
- 真实前端函数的受控异步竞态测试：1项通过。
- 另一任务的路由诊断/策略CPU测试：9项通过。
- 隔离浏览器：缓存审计9项、路由诊断7项通过。
- 最后一次内容边界修改额外执行13项内容契约与7项review用例，20项通过。

验证入口为 `tests/test_audit_review_regressions.py`、`tests/test_cache_review_metrics.py`、`tests/test_gateway_review_telemetry.py`、`tests/test_cache_overview_race.cjs`。浏览器仅连接标明isolated的本机CPU预览服务。

证据保存在AI的 `experiments/cache-audit-20260907`：`review-reproduced-before.txt`、`review-null-tools-before.txt`、`review-regressions-after.txt`、`review-integrated-cpu.txt`、`review-content-final.txt`、`review-diagnosis-policy-tests.txt`、`browser_cache_audit-review-final.txt`、`browser_route_diagnosis-review-final.txt`，以及 `review-evidence/` 的独立复现。

上述验证证明修复逻辑与合并兼容性，不替代NX3/AGX后续自然请求的原生缓存计数验收。本次提交过程不部署或重启生产服务。
