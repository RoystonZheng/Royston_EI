# Demo复线

## 准备工作
我当时让 Codex 协助配置环境、整理运行脚本和执行测试，先把不需要 API 的部分跑通，再接大模型。两个项目放在下面的目录：
- [AIFP-main](/Users/didi/Documents/personalcodexfile/ei/项目复现/AIFP-main)
- [GridRoute-main](/Users/didi/Documents/personalcodexfile/ei/项目复现/GridRoute-main)

这次实际用的是独立的 Python `.venv` 环境，Python 版本是 3.9.6。环境准备的操作是创建虚拟环境，再安装 `requirements-repro.txt` 里的依赖，包括 NumPy、Pandas、Matplotlib、Shapely 和 OpenAI SDK 等。
```bas
cd "/Users/didi/Documents/personalcodexfile/ei"
uv venv --python /usr/bin/python3 .venv
uv pip install --python .venv/bin/python -r requirements-repro.txt
```
下面的运行命令使用同一个环境：
```bas
PY="/Users/didi/Documents/personalcodexfile/ei/.venv/bin/python"
```
我最终使用的是 **OpenRouter 平台的 API Key**，通过它调用 DeepSeek 和 OpenAI 的模型。

| 项目 | 最终保留结果使用的模型 | 接口地址 |
|---|---|---|
| AIFP | `deepseek/deepseek-chat-v3-0324`，DeepSeek V3 | `https://openrouter.ai/api/v1` |
| GridRoute | `openai/gpt-oss-20b`，OpenAI GPT-OSS-20B | `https://openrouter.ai/api/v1` |
## 一、AIFP

AIFP 是让一个机器人在二维地图里绕过障碍物，从起点走到目标点。
接入大模型以后，程序把当前位置、目标位置和障碍物坐标放进提示词，让模型提出接下来的几个位置。程序用 Shapely 检查候选点和当前位置之间的连线，选出能走的点往前推进。如果没有合适的点，就给下一次提示加上失败反馈，让模型重新规划。结束后，还会检查整条轨迹是否碰撞。
我理解这里的大模型负责提议怎么走，几何检查由程序完成。

### 我先跑了不接 API 的版本

我运行 `run_repro.py`，分别调用 A*、RRT、MCTS 和 Q-learning。它们是四种不依赖大模型 API 的规划方法，不是 AIFP 内部必须依次经过的四个步骤。
这次用的是保存图片的运行方式，同时记录日志。

| 方法 | 我拿到的结果 | 当次日志里的耗时 |
|---|---|---:|
| A* | 脚本运行完成，生成路径图 | 约 1.7 秒 |
| RRT | 脚本运行完成，生成路径图 | 约 0.1 秒 |
| MCTS | 脚本运行完成，生成路径图 | 约 1.5 秒 |
| Q-learning | 脚本运行完成，生成路径图 | 约 7.4 秒 |

### 我再接入 DeepSeek
我把模型设成 DeepSeek V3，使用 `run_llm_planners.py aifp` 运行 AIFP 主流程，并把每次运行的试验数设为 1。按当前脚本复跑这一流程的命令是：
程序会先测试 API 能否连通，再开始规划。每轮让模型给出 5 个候选位置，最多执行其中 2 个经过筛选的点，下一轮继续根据新位置规划。这条命令会重新调用 API，结果不保证和之前一样。
| 项目 | 结果 |
|---|---:|
| 是否到达目标 | 是 |
| 规划迭代次数 | 33 |
| 规划过程中的 LLM 调用次数 | 33 |
| 重规划次数 | 11 |
| JSON 解析失败次数 | 0 |
| 路径点数 | 42 |
| 最后一个点到目标的距离 | 0.100，地图坐标单位 |
| 整条路径的碰撞检查 | 无碰撞 |
| 当次规划耗时 | 约 127.8 秒 |

33 次是规划过程中记录的调用次数，前面的连通性测试另算。“到达”按程序设定的坐标容差判断，不要求最后一个点和目标坐标完全相同。
调试过程中也有其他结果：AIFP 最初尝试 OpenAI GPT-OSS-20B 时，接口能连通，但返回内容的处理触发了 `TypeError`，那次没有完成规划；DeepSeek 有一次运行 36 轮后到达目标，耗时约 144.6 秒；还有一次只允许跑 8 轮，到上限时没有到达。这些和最后保留的 33 轮结果是不同次运行，不能混成一次，也不能拿最后一次成功说总体成功率就是 100%。
我看到的成功路线图在 [aifp_deepseek_success.png](/Users/didi/Documents/personalcodexfile/ei/第二次课Demo复现提交包/AIFP/OpenRouter_API结果/aifp_deepseek_success.png)，对应记录在 [api_run_summary.txt](/Users/didi/Documents/personalcodexfile/ei/第二次课Demo复现提交包/AIFP/OpenRouter_API结果/api_run_summary.txt)。

### 对比
这次 API 版本能完成绕障，但用了多轮调用。无 API 脚本的运行时间更短，不过当前经典脚本的起终点是 `(-1.5, 1.2)` 到 `(0.5, -0.25)`，LLM 脚本是 `(1, -0.5)` 到 `(-1.5, 1.2)`，条件并不完全一致。我通过这个 Demo 理解的是：LLM 可以提出路线，程序需要检查，走不通时再根据反馈调整。

## 二、GridRoute
GridRoute 给出一张网格地图、起点、终点和障碍物，要求每一步只能上下左右移动一格。它主要用来测试模型有没有输出路径、路径是否符合规则，以及长度和参考最短路径差多少。Dijkstra 负责生成参考答案。模型输出后，评估程序检查起终点、越界、障碍物和移动方式，再和参考答案比较。它也是路径规划 Demo，还没有处理多辆车的订单调度。

### 我先跑了数据和参考路径
数据生成脚本是 `src/data_generation.py`，会生成不同尺寸的障碍布局和起终点任务，保存成 `data/dataset.csv`。这次保留的数据有 1500 条任务，10×10、20×20、30×30 各 500 条。
生成数据的入口是下面这条命令。它会重写数据文件；接着复用已有结果时，我使用保留的 `dataset.csv`，不重新生成另一批数据。
```bash
cd "/Users/didi/Documents/personalcodexfile/ei/项目复现/GridRoute-main"
"$PY" -m src.data_generation
```
我再用 Dijkstra 生成 `reference.csv`，然后把参考路径本身交给评估程序做一次自检：
```bash
"$PY" -m src.reference_paths
"$PY" -m src.Indicator_evaluation \
  --api data/reference.csv \
  --reference data/reference.csv \
  --dataset data/dataset.csv \
  --output results/overall_reference_self_check.csv
```
这一步的结果是 1500 条路径全部生成、全部有效，长度也全部和参考答案相同。

### 接入模型
我在 GridRoute 里也试过通过 OpenRouter 调用 DeepSeek V3，但当时保存的输出混有解释文字或代码，没有得到评估器能使用的完整路径。之后改用 OpenAI GPT-OSS-20B，采用 `algorithm` 提示词，把算法步骤放进提示里。我先跑通了 1 条 API 样例，然后又对同一批前 10 条任务做了小样本测试，避免直接跑完整数据集。10 条测试时设置的输出上限是 256 token，超时设置为 90 秒；程序层面对每条任务最多尝试 3 轮请求。
```bash
cd "/Users/didi/Documents/personalcodexfile/ei/项目复现/GridRoute-main"
export OPENROUTER_MODEL="openai/gpt-oss-20b"
export OPENROUTER_TIMEOUT="90"
export OPENROUTER_MAX_TOKENS="256"
"$PY" -u -m src.api_experiment \
  --model "openai/gpt-oss-20b" \
  --template algorithm \
  --limit 10 \
  --output results/openrouter_openai_gpt_oss_20b_algorithm_limit10.csv
```
这个最多 3 轮是脚本中的重试次数，不是已核对过的计费请求总数，SDK 遇到部分网络错误时还可能重试。运行后，我把模型输出和相同行号的地图、参考路径一起交给评估器：
```bash
"$PY" -u -m src.Indicator_evaluation \
  --api results/openrouter_openai_gpt_oss_20b_algorithm_limit10.csv \
  --reference data/reference.csv \
  --dataset data/dataset.csv \
  --output results/openrouter_openai_gpt_oss_20b_algorithm_limit10_metrics.csv
```
### 结果
先前单独保留的 1 条成功样例，路径有效，长度与参考最短路径相同，耗时约 53.43 秒。它是单独一次运行，和后面的 10 条测试分开记录。
后面的 10 条任务全部是 10×10 网格，涉及两组障碍布局和不同起终点。结果如下：
| 方法 | 测试任务数 | 解析出路径 | 有效路径 | 达到参考最短长度 |
|---|---:|---:|---:|---:|
| Dijkstra 参考路径自检 | 10 | 10 | 10 | 10 |
| OpenRouter / OpenAI GPT-OSS-20B | 10 | 3 | 3 | 3 |

LLM 的 7 条失败结果保存为 `[]`，没有拿到可评估的路径。仅凭这份 CSV，不能确定每条失败都是不会寻路，也可能涉及返回内容、输出长度或解析环节；但它们在这次测试里都没有完成任务。

指标文件里的结果是：
- **CR = 30%：** 10 条任务中，3 条解析出了非空路径。
- **FR = 30%：** 10 条任务中，3 条路径通过合法性检查。
- **OR = 100%：** 只在 3 条有效路径中统计，这 3 条都达到参考最短长度。放回全部任务看，完成且最优的是 3/10。
- **GM = 1.0、MSE = 0.0：** 这次可比较的有效路径，长度都和参考路径相同，不代表坐标走法必须一模一样。
- **全部 10 条任务平均耗时约 15.21 秒：** 按原始结果 CSV 的每条任务耗时计算，包含脚本内部的尝试过程。
- **成功输出的 3 条平均耗时约 13.68 秒：** 指标文件的 `Average Time` 使用的是这一口径。
原始结果在 [10 条 API 输出](/Users/didi/Documents/personalcodexfile/ei/第二次课Demo复现提交包/GridRoute/对比结果/openrouter_openai_gpt_oss_20b_algorithm_limit10.csv)，评估结果在 [10 条 API 指标](/Users/didi/Documents/personalcodexfile/ei/第二次课Demo复现提交包/GridRoute/对比结果/openrouter_openai_gpt_oss_20b_algorithm_limit10_metrics.csv)。
我还把路径画成图片，查看起点、终点、障碍物和路线有没有对应上。单条成功样例路线图在 [path_map_1.png](/Users/didi/Documents/personalcodexfile/ei/第二次课Demo复现提交包/GridRoute/OpenRouter_API成功样例/path_map_1.png)。

### 理解

我需要分清 API 连通、模型返回内容、程序解析出路径、路径合法、路径达到最短长度，这几件事不一样。GridRoute 把这些结果分别检查出来，所以不能只看到模型回复了，或者看到 OR 是 100%，就说大模型全部做对了。
