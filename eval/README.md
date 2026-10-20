# 评测样本

这里是代码审查工作台的评测骨架。`samples/` 里有 5 个带已知缺陷的 Python 文件，每个文件第一行注明预期缺陷。`run_stats.py` 读取可选的 JSON 结果并打印召回占位，不调用模型。

```bash
python eval/run_stats.py
python eval/run_stats.py eval/results.json
```

结果文件可以是对象或列表。脚本只报告是否读到结果，不计算真实召回。
