# 十种训练技巧如何用于本项目

本项目训练出来的是 PyTorch `.pt` 文件。训练入口是：

```bash
python ai/train_all.py
```

训练代码在：

```text
ai/train_common.py
ai/models.py
```

## 先创建虚拟环境

在项目根目录运行：

```bash
python3 setup_env.py
```

Windows：

```bash
py setup_env.py
```

激活虚拟环境。

macOS / Linux：

```bash
source .venv/bin/activate
```

Windows：

```bash
.venv\Scripts\activate
```

确认已经进入虚拟环境：

```bash
python -c "import sys; print(sys.prefix)"
```

输出路径里应该包含 `.venv`。

## 一次训练四个 .pt

```bash
python ai/train_all.py
```

训练完成后会生成：

```text
ai/checkpoints/dijkstra_ai.pt
ai/checkpoints/astar_dynamic_ai.pt
ai/checkpoints/rrt_narrow_ai.pt
ai/checkpoints/car_control_ai.pt
ai/training_summary.json
```

然后导出网页用的数据：

```bash
python ai/export_rollouts.py
```

再打开 `index.html`，右侧 AI 面板会显示 `.pt 已生成`。

## 十种技巧对应关系

```text
1. Advantage Normalization
把奖励信号变成优势值，再归一化，用它给关键样本加权。

2. State Normalization
训练前把输入状态做均值/方差归一化，避免坐标、距离、障碍距离量纲不同导致训练不稳。

3. Reward Normalization
把样本奖励标准化，避免奖励数值太大或太小。

4. Reward Scaling
先按 reward_scale 缩放奖励，让奖励落在更温和的范围。

5. Policy Entropy
分类策略用 Categorical entropy，连续控制用 Normal entropy，防止策略过早变得太死。

6. Learning Rate Decay
使用 CosineAnnealingLR，让学习率从大到小衰减。

7. Gradient Clip
每次反向传播后裁剪梯度，避免梯度爆炸。

8. Orthogonal Initialization
Linear 层使用正交初始化，让初始网络更稳定。

9. Adam Optimizer Epsilon Parameter
Adam 使用 eps=1e-5，减少除以极小数造成的抖动。

10. Tanh Activation Function
隐藏层使用 Tanh；连续控制输出也用 Tanh 限制在 -1 到 1。
```

## 单独训练某个模型

```bash
python ai/train_dijkstra_ai.py
python ai/train_astar_dynamic_ai.py
python ai/train_rrt_narrow_ai.py
python ai/train_car_control_ai.py
```

## 训练时你应该观察什么

```text
loss：总体训练目标，越低越好。
supervised_loss：模仿专家动作的损失。
entropy：策略探索程度，不能一开始就太低。
learning_rate：学习率会逐步下降。
accuracy：Dijkstra / A* 动态障碍这类离散动作任务的动作预测准确率。
mse：RRT / 小车控制这类连续动作任务的误差。
```

## 一句话理解

这十种技巧不是让 AI 一定比传统算法强，而是让训练过程更稳定：输入先拉到同一尺度，奖励和优势不乱跳，策略保留一点探索，学习率慢慢降，梯度不爆，网络初始化也更稳。
