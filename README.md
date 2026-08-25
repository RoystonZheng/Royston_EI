# 基础路径规划算法缺陷 Demo

这个项目用来对比基础路径规划算法和 AI 训练策略。页面里每个实验都是左侧传统算法、右侧 AI 训练侧，并且会展示搜索或轨迹留痕。

## 直接打开网页

在 VSCode 里打开这个文件夹，然后打开：

```text
index.html
```

如果装了 VSCode 的 Live Server 插件，也可以右键 `index.html`，选择 `Open with Live Server`。

## 四个实验

页面上有 `普通 / 困难 / 极限` 三档强度。

```text
Dijkstra vs AI：Dijkstra 会大范围扩散；AI 训练后直接输出动作轨迹。
A* vs AI：A* 在动态障碍里会反复重规划；AI 侧展示带等待动作的策略轨迹。
RRT vs AI：RRT 在窄通道里靠随机采样；AI 侧把采样集中到窄门附近。
网格路径 vs AI：网格路径不考虑车辆转弯半径；AI 侧展示连续转向控制轨迹。
```

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
网格规划：能找到格子路径，但不等于真实车辆能执行。
```

它不证明 AI 永远更好。AI 也有训练成本高、依赖样本、泛化不稳定、解释性弱的问题。
