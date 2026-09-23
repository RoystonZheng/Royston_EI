# 第二次课 Demo 复现与 OpenRouter API 接入

姓名：郑睿涛  
复现日期：2026-09-23

## 1. 本次完成了什么

本次复现包含两个 Demo：

1. AIFP：单个机器人在连续坐标地图中绕开障碍物到达目标点。
2. GridRoute：在网格地图中，从起点走到终点，并评估路径是否合法、是否达到参考最优长度。

本次没有进行模型训练，也没有运行 1500 次付费 API 请求。1500 张地图的结果是 Dijkstra 生成的参考路径和评估程序自检；真实 API 只运行了小规模样例。

## 2. AIFP 的运行结果

### 2.1 经典算法

运行了 A*、RRT、MCTS 和 Q-learning：

| 算法 | 结果 | 耗时 |
|---|---|---:|
| A* | 成功生成路径图 | 约 1.7 秒 |
| RRT | 成功生成路径图 | 约 0.1 秒 |
| MCTS | 成功生成路径图 | 约 1.5 秒 |
| Q-learning | 成功生成路径图 | 约 7.4 秒 |

图片在 `AIFP/经典算法结果/`，运行日志在同目录的 `classical_run.log`。

### 2.2 OpenRouter API

AIFP 使用 OpenRouter 的 DeepSeek V3 模型运行了一次完整试验：

```text
模型：deepseek/deepseek-chat-v3-0324
试验次数：1
迭代次数：33
LLM 调用次数：33
重规划次数：11
JSON 解析失败：0
路径点数：42
终点距离：0.100
全路径无碰撞：True
耗时：约 127.8 秒
到达目标：是
```

路径图片在 `AIFP/OpenRouter_API结果/`。

## 3. GridRoute 的运行结果

### 3.1 1500 张参考路径

`data/dataset.csv` 包含 1500 张地图。程序使用 Dijkstra 为每张地图生成参考路径，输出 `reference.csv`。

自检结果：

```text
Total cases: 1500
Generated paths: 1500 (100.00%)
Valid paths: 1500 (100.00%)
Optimal valid paths: 1500
```

这里的 100% 不是大模型准确率，因为评估输入和参考答案都是 Dijkstra 生成的 `reference.csv`。它只能证明数据、Dijkstra、路径校验和评估程序能够正常工作。

### 3.2 OpenRouter API 样例

GridRoute 使用 OpenRouter 的 OpenAI GPT-OSS-20B 跑了 1 张地图，结果如下：

```text
模型：openai/gpt-oss-20b
提示词模板：algorithm
地图数量：1
生成路径：1
有效路径：1
最优有效路径：1
CR：1.0
FR：1.0
OR：1.0
GM：1.0
MSE：0.0
耗时：约 53.43 秒
```

结果 CSV、指标 CSV 和路线图在 `GridRoute/OpenRouter_API成功样例/`。

这只是 1 张地图的链路验收，不代表 1500 张地图上的总体性能。

## 4. OpenRouter 接入方式

OpenRouter 使用 OpenAI SDK 兼容接口：

```text
URL：https://openrouter.ai/api/v1
```

本包不包含真实 API Key。需要使用时，在本地的 `openrouter.env` 中填写：

```bash
export OPENROUTER_API_KEY="你的真实 Key"
export OPENROUTER_BASE_URL="https://openrouter.ai/api/v1"
export OPENROUTER_MODEL="deepseek/deepseek-chat-v3-0324"
```

然后执行：

```bash
source "/Users/didi/Documents/personalcodexfile/ei/openrouter.env"
```

真实 Key 只能放在 `openrouter.env`，不能放进 `.py` 文件，也不能上传到 Git。

## 5. 端到端流程

### AIFP

```text
起点、终点、障碍物
        ↓
生成提示词
        ↓
OpenRouter 模型返回候选位置
        ↓
解析 JSON
        ↓
检查路线是否穿过障碍物
        ↓
如果无效，加入碰撞反馈并重新规划
        ↓
绘制路径图并统计结果
```

### GridRoute

```text
dataset.csv
        ↓
Dijkstra 生成 reference.csv
        ↓
模型根据地图描述输出坐标路径
        ↓
解析路径
        ↓
检查越界、碰撞、起点、终点和移动规则
        ↓
与 reference.csv 比较
        ↓
输出 CR、FR、OR、GM、MSE 等指标
```

## 6. 本次结果的边界

1. AIFP 和 GridRoute 都是单车/单机器人路径规划 Demo，不是多智能体无人车调度系统。
2. GridRoute 的 1500 条 100% 结果是 Dijkstra 参考答案自检，不是 LLM 结果。
3. API 样例只运行了少量请求，没有未经确认地运行 1500 次 API。
4. 真实 API 结果依赖模型版本、OpenRouter 供应商、网络和账户余额，后续复跑数值可能变化。
