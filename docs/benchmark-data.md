# Benchmark 数据与比较工作流

## Benchmark 导入格式

比较命令只使用人工核验或权威来源采集的 benchmark 结果，不从厂商定位文案推断分数。文件格式：

```json
{
  "benchmarks": [
    {
      "provider": "Example Provider",
      "model": "example-model",
      "benchmark_id": "coding-suite",
      "benchmark_version": "1",
      "task": "coding",
      "score": 82.5,
      "max_score": 100,
      "source_url": "https://example.com/benchmark-results",
      "observed_at": "2026-11-03T00:00:00Z",
      "protocol": "public-v1"
    }
  ]
}
```

`source_url` 和带时区的 `observed_at` 必填。分数须落在 `[0, max_score]`。没有完全相同任务、benchmark ID、版本和协议的数据时，候选会标记“未评级”。严禁跨 benchmark 拼成一个相对分。

价格成本按本次 `input_tokens` 与 `output_tokens` 的确切数量计算；默认不假设固定输入输出比例。最高 benchmark 分数映射为本候选集合的 100 分。零价格模型排在性价比序列最前，不生成无穷大 JSON 数字。

严格支配只在可比候选间判断：候选 A 性能严格更高且本次估算成本严格更低时，才标记 B 被 A 支配。档位采用相对分：旗舰 ≥85，主力 ≥65，其他为轻量。这是当前集合内的业务标签，不代表跨任务通用质量门槛。

## 持久化五节点

```text
collect → quantify → analyze → recommend → assemble
```

`compare` 开始时冻结当时的报价与 benchmark 输入快照。每个成功节点将输出存为检查点并写事件；失败时记录节点和错误。`workflow resume RUN_ID` 从第一个未完成节点继续，使用原始快照，不会悄悄换成更新后的市场数据。`workflow status RUN_ID` 查看状态、检查点与事件。每个 run 有跨进程文件锁避免双重执行，进程异常退出后由操作系统释放锁；节点最多执行四次（首次执行加三次恢复），统计开始事件，因此进程崩溃也计入执行次数；超限后需修复输入并创建新 run。当前是单机 SQLite 工作流，不能多个机器共享 SQLite 文件。

## 示例

```powershell
token-mentor import-benchmarks --file benchmarks.json
token-mentor compare --task coding --input-tokens 4000 --output-tokens 1200 `
  --benchmark-id coding-suite --benchmark-version 1 --protocol public-v1 `
  --output reports/coding-comparison.md
token-mentor workflow status <RUN_ID>
token-mentor workflow resume <RUN_ID>
```

`compare` 会读取当前最新报价并输出 Markdown；如果 benchmark 证据不足，仍输出成本数据，但不臆造性能排名或场景推荐。
