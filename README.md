# 基础路径规划算法缺陷 Demo

这个项目用来对比基础路径规划算法和 AI 训练策略。页面里每个实验都是左侧传统算法、右侧 AI 训练侧，并且会展示搜索或轨迹留痕。

## 直接打开网页

在 VSCode 里打开这个文件夹，然后打开：

```text
index.html
```

如果装了 VSCode 的 Live Server 插件，也可以右键 `index.html`，选择 `Open with Live Server`。

控制按钮含义：

```text
运行：自动生成一张新地图，并让 AI 进行一轮演示训练。
暂停：暂停当前地图，不清空进度。
单步：手动推进当前地图。
重置：回到当前地图的初始状态，不换图。
重置训练：清空当前实验、当前难度、当前环境模式下的浏览器演示训练成果。
```

## 四个实验

页面上有 `普通 / 困难 / 极限` 三档强度，也有三种环境模式：

```text
训练场景：和训练样本接近，用来观察基本算法现象。
扰动场景：障碍位置、障碍速度、小车初始姿态发生小变化，用来观察鲁棒性。
泛化场景：换成训练外布局，障碍更密、速度更快、小车更大，用来观察泛化性。
```

```text
Dijkstra vs AI：Dijkstra 会大范围扩散；地图会刷新，并加入移动障碍和机器人半径显示。
A* vs AI：A* 在动态障碍里会反复重规划；机器人带半径，动态障碍更容易让路径失效。
RRT vs AI：RRT 在窄通道里靠随机采样；移动矩形障碍和机器人安全半径会进一步压缩可通行空间。
网格路径 vs AI：网格路径不考虑车辆面积和严格转弯半径；走廊里会出现移动障碍，AI 侧展示连续转向控制轨迹。
```

所有实验画面都是动态的：左侧会显示搜索扩散、重规划、采样树、小车跟踪等过程，右侧会显示 AI 轨迹或采样点随时间推进。传统算法和 AI 侧独立结束，一边完成后另一边会继续执行到自己的终点或失败状态。

## 训练 .pt 文件

仓库不上传 `.venv`。换一台设备后，先运行这个 Python 文件，它会在本地生成虚拟环境并安装依赖：

```bash
python3 setup_env.py
```

Windows 可以用：

```bash
py setup_env.py
```

然后激活环境：

```bash
source .venv/bin/activate
```

Windows：

```bash
.venv\Scripts\activate
```

一次训练四个模型：

```bash
python ai/train_all.py
```

默认训练会混合 `普通 / 困难 / 极限` 三档样本。

十种训练技巧的具体用法看：

```text
TRAINING_TEN_TRICKS.md
```

实验设计和给老师讲解的逻辑看：

```text
EXPERIMENT_DESIGN.md
```

训练完成后会生成：

```text
ai/checkpoints/dijkstra_ai.pt
ai/checkpoints/astar_dynamic_ai.pt
ai/checkpoints/rrt_narrow_ai.pt
ai/checkpoints/car_control_ai.pt
ai/training_summary.json
```

再把训练结果导出给网页：

```bash
python ai/export_rollouts.py
```

这个命令会更新：

```text
src/ai_rollouts.js
ai/rollouts/*.json
src/ai_rollouts/*.json
```

如果你想强制使用锁定版本依赖，可以运行：

```bash
python3 setup_env.py --locked
```

## 单独训练某个模型

```bash
python ai/train_dijkstra_ai.py
python ai/train_astar_dynamic_ai.py
python ai/train_rrt_narrow_ai.py
python ai/train_car_control_ai.py
```

## 本地检查

```bash
node --check src/main.js
node scripts/ui_smoke_test.js
PYTHONPYCACHEPREFIX=/private/tmp/path-planning-demo-pycache python3 -m py_compile setup_env.py ai/*.py
```

## Git 说明

`.venv/` 不会上传到 Git。训练出来的 `.pt` 没有被忽略，如果你训练完成后想把模型也传到仓库，可以正常执行：

```bash
git add ai/checkpoints/*.pt ai/training_summary.json src/ai_rollouts.js ai/rollouts src/ai_rollouts
git commit -m "Add trained AI checkpoints"
git push
```

## 这个 demo 证明什么

它证明的是基础算法在特定条件下的短板：

```text
Dijkstra：没有方向感，搜索范围大。
A*：静态场景很强，但动态障碍会让路径频繁失效。
RRT：能处理连续空间，但窄通道里随机性强、不稳定。
网格规划：能找到点路径，但不等于有面积的小车能执行。
```

每个页面右侧都有 `鲁棒性 / 泛化性 / 可解释性` 小结。它不证明 AI 永远更好，AI 也有训练成本高、依赖样本、泛化不稳定、解释性弱的问题；这个项目展示的是为什么在真实小车、自动驾驶或机器人场景里，工程上经常需要把学习框架、车体面积和运动约束一起考虑进去。
