(() => {
  "use strict";

  const canvas = document.getElementById("labCanvas");
  const ctx = canvas.getContext("2d");
  const demoTitle = document.getElementById("demoTitle");
  const demoClaim = document.getElementById("demoClaim");
  const metricsEl = document.getElementById("metrics");
  const drawbacksEl = document.getElementById("drawbacks");
  const evaluationEl = document.getElementById("evaluation");
  const legendEl = document.getElementById("legend");
  const secondaryBtn = document.getElementById("secondaryBtn");
  const runBtn = document.getElementById("runBtn");
  const pauseBtn = document.getElementById("pauseBtn");
  const stepBtn = document.getElementById("stepBtn");
  const resetBtn = document.getElementById("resetBtn");
  const trainingResetBtn = document.getElementById("trainingResetBtn");
  const speedSlider = document.getElementById("speedSlider");
  const tabButtons = Array.from(document.querySelectorAll(".tab"));
  const difficultyButtons = Array.from(document.querySelectorAll(".difficulty"));
  const scenarioButtons = Array.from(document.querySelectorAll(".scenario"));

  const COLOR = {
    bg: "#0e1117",
    panel: "#151a22",
    panelLine: "#303846",
    gridLine: "rgba(255,255,255,0.05)",
    obstacle: "#2a313d",
    start: "#7bd88f",
    goal: "#ef6461",
    visited: "#4fa3ff",
    visitedAlt: "#b78cff",
    path: "#ffd166",
    oldPath: "#7f8a9a",
    dynamic: "#f79824",
    actual: "#35d0c3",
    text: "#eef2f6",
    muted: "#9aa6b2",
    failure: "#ef6461",
  };

  const state = {
    viewW: 0,
    viewH: 0,
    dpr: 1,
    activeId: "search",
    activeDemo: null,
    lastTime: 0,
    speed: Number(speedSlider.value),
    difficulty: "hard",
    scenario: "baseline",
  };

  const DIFFICULTY = {
    normal: { label: "普通", multiplier: 1 },
    hard: { label: "困难", multiplier: 1.45 },
    extreme: { label: "极限", multiplier: 2.1 },
  };

  const SCENARIO = {
    baseline: {
      label: "训练场景",
      summary: "与训练样本接近，用来观察基本算法现象",
      obstacleShift: 0,
      randomBlocks: 0,
      speedMultiplier: 1,
      latencyMs: 0,
      sensorNoise: 0,
      carScale: 1,
      generalization: 0,
    },
    perturbed: {
      label: "扰动场景",
      summary: "障碍位置、速度和小车姿态都有小幅变化",
      obstacleShift: 3,
      randomBlocks: 8,
      speedMultiplier: 1.28,
      latencyMs: 120,
      sensorNoise: 0.18,
      carScale: 1.12,
      generalization: 0.35,
    },
    generalization: {
      label: "泛化场景",
      summary: "换成训练外布局，检验模型和算法能不能迁移",
      obstacleShift: 6,
      randomBlocks: 14,
      speedMultiplier: 1.55,
      latencyMs: 260,
      sensorNoise: 0.32,
      carScale: 1.24,
      generalization: 0.7,
    },
  };

  const AI_ROLLOUTS = window.AI_ROLLOUTS || {};
  const runtimeTraining = {};
  let globalMapSerial = 0;

  function getAiRollout(task, difficulty) {
    return AI_ROLLOUTS[task]?.[difficulty] || AI_ROLLOUTS[task]?.hard || null;
  }

  function checkpointLabel(rollout) {
    return rollout?.training?.hasCheckpoint ? ".pt 已生成" : ".pt 待训练";
  }

  function trainingKey(task, difficulty, scenario) {
    return `${task}:${difficulty}:${scenario}`;
  }

  function getRuntimeTraining(task, difficulty, scenario) {
    const key = trainingKey(task, difficulty, scenario);
    if (!runtimeTraining[key]) {
      runtimeTraining[key] = {
        rounds: 0,
        loss: 1,
        successRate: 0.35,
      };
    }
    return runtimeTraining[key];
  }

  function trainRuntimeAi(task, difficulty, scenario) {
    const stateValue = getRuntimeTraining(task, difficulty, scenario);
    const difficultyPenalty = difficulty === "extreme" ? 0.04 : difficulty === "hard" ? 0.025 : 0.01;
    const scenarioPenalty = scenarioConfig(scenario).generalization * 0.06;
    stateValue.rounds += 1;
    stateValue.loss = Math.max(0.05, stateValue.loss * (0.76 + difficultyPenalty + scenarioPenalty));
    stateValue.successRate = Math.min(0.98, stateValue.successRate + 0.11 - difficultyPenalty - scenarioPenalty * 0.45);
    return stateValue;
  }

  function resetRuntimeTraining(task, difficulty, scenario) {
    const key = trainingKey(task, difficulty, scenario);
    runtimeTraining[key] = {
      rounds: 0,
      loss: 1,
      successRate: 0.35,
    };
    return runtimeTraining[key];
  }

  function trainingMetricRows(trainingState) {
    return [
      ["AI 训练轮次", trainingState?.rounds ?? 0],
      ["AI 当前 loss", formatNumber(trainingState?.loss ?? 1, 3)],
      ["AI 成功率估计", `${formatNumber((trainingState?.successRate ?? 0.35) * 100, 1)}%`],
    ];
  }

  function nextMapIdentity(task, difficulty, scenario) {
    globalMapSerial += 1;
    return {
      serial: globalMapSerial,
      seed: scenarioSeed(`map:${task}`, difficulty, scenario, globalMapSerial),
    };
  }

  function ensureMapIdentity(demo, forceNew = false) {
    if (forceNew || !demo.mapSeed) {
      const identity = nextMapIdentity(demo.demoId, demo.difficulty, demo.scenario);
      demo.mapSerial = identity.serial;
      demo.mapSeed = identity.seed;
    }
  }

  function statusLabel(isRunning, isDone, isFailed = false) {
    if (isFailed) return "失败";
    if (isRunning) return "运行中";
    if (isDone) return "完成";
    return "待运行";
  }

  function progressSlice(points, progress) {
    if (!points || points.length === 0) return [];
    const count = clamp(Math.floor(progress), 1, points.length);
    return points.slice(0, count);
  }

  class PriorityQueue {
    constructor() {
      this.heap = [];
    }

    push(item, priority) {
      this.heap.push({ item, priority });
      this.bubbleUp(this.heap.length - 1);
    }

    pop() {
      if (this.heap.length === 0) return null;
      const top = this.heap[0];
      const last = this.heap.pop();
      if (this.heap.length > 0) {
        this.heap[0] = last;
        this.sinkDown(0);
      }
      return top.item;
    }

    get size() {
      return this.heap.length;
    }

    bubbleUp(index) {
      while (index > 0) {
        const parent = Math.floor((index - 1) / 2);
        if (this.heap[parent].priority <= this.heap[index].priority) break;
        [this.heap[parent], this.heap[index]] = [this.heap[index], this.heap[parent]];
        index = parent;
      }
    }

    sinkDown(index) {
      while (true) {
        const left = index * 2 + 1;
        const right = index * 2 + 2;
        let smallest = index;
        if (left < this.heap.length && this.heap[left].priority < this.heap[smallest].priority) {
          smallest = left;
        }
        if (right < this.heap.length && this.heap[right].priority < this.heap[smallest].priority) {
          smallest = right;
        }
        if (smallest === index) break;
        [this.heap[smallest], this.heap[index]] = [this.heap[index], this.heap[smallest]];
        index = smallest;
      }
    }
  }

  function idx(x, y, w) {
    return y * w + x;
  }

  function cellFromId(id, w) {
    return { x: id % w, y: Math.floor(id / w) };
  }

  function createGrid(w, h, fill = 0) {
    return new Uint8Array(w * h).fill(fill);
  }

  function setCell(grid, w, x, y, value) {
    if (x >= 0 && y >= 0 && x < w && y < grid.length / w) {
      grid[idx(x, y, w)] = value;
    }
  }

  function fillRectCells(grid, w, h, x, y, rw, rh, value) {
    for (let yy = y; yy < y + rh; yy += 1) {
      for (let xx = x; xx < x + rw; xx += 1) {
        if (xx >= 0 && yy >= 0 && xx < w && yy < h) setCell(grid, w, xx, yy, value);
      }
    }
  }

  function isGridBlocked(grid, w, h, x, y, extraBlocked) {
    if (x < 0 || y < 0 || x >= w || y >= h) return true;
    if (grid[idx(x, y, w)] === 1) return true;
    return extraBlocked ? extraBlocked(x, y) : false;
  }

  function manhattan(a, b) {
    return Math.abs(a.x - b.x) + Math.abs(a.y - b.y);
  }

  function buildPath(cameFrom, startId, goalId, w) {
    if (cameFrom[goalId] === -1 && startId !== goalId) return [];
    const ids = [];
    let current = goalId;
    ids.push(current);
    while (current !== startId) {
      current = cameFrom[current];
      if (current === -1) return [];
      ids.push(current);
    }
    ids.reverse();
    return ids.map((idValue) => cellFromId(idValue, w));
  }

  function gridSearch({ grid, w, h, start, goal, heuristic, extraBlocked }) {
    const started = performance.now();
    const startId = idx(start.x, start.y, w);
    const goalId = idx(goal.x, goal.y, w);
    const dist = new Float64Array(w * h);
    const cameFrom = new Int32Array(w * h);
    const closed = new Uint8Array(w * h);
    const visitedOrder = [];
    const open = new PriorityQueue();
    dist.fill(Number.POSITIVE_INFINITY);
    cameFrom.fill(-1);
    dist[startId] = 0;
    open.push(startId, heuristic(start, goal));

    const dirs = [
      [1, 0],
      [-1, 0],
      [0, 1],
      [0, -1],
    ];

    while (open.size > 0) {
      const currentId = open.pop();
      if (closed[currentId]) continue;
      closed[currentId] = 1;
      const current = cellFromId(currentId, w);
      visitedOrder.push(current);
      if (currentId === goalId) break;

      for (const [dx, dy] of dirs) {
        const nx = current.x + dx;
        const ny = current.y + dy;
        if (isGridBlocked(grid, w, h, nx, ny, extraBlocked)) continue;
        const neighborId = idx(nx, ny, w);
        if (closed[neighborId]) continue;
        const nextDist = dist[currentId] + 1;
        if (nextDist < dist[neighborId]) {
          dist[neighborId] = nextDist;
          cameFrom[neighborId] = currentId;
          const priority = nextDist + heuristic({ x: nx, y: ny }, goal);
          open.push(neighborId, priority);
        }
      }
    }

    const path = buildPath(cameFrom, startId, goalId, w);
    return {
      success: path.length > 0,
      visitedOrder,
      path,
      pathLength: Math.max(0, path.length - 1),
      timeMs: performance.now() - started,
    };
  }

  function countTurns(path) {
    let turns = 0;
    let previous = null;
    for (let i = 1; i < path.length; i += 1) {
      const direction = {
        x: Math.sign(path[i].x - path[i - 1].x),
        y: Math.sign(path[i].y - path[i - 1].y),
      };
      if (previous && (previous.x !== direction.x || previous.y !== direction.y)) turns += 1;
      previous = direction;
    }
    return turns;
  }

  function formatNumber(value, digits = 0) {
    if (Number.isNaN(value) || value === undefined || value === null) return "-";
    return Number(value).toFixed(digits);
  }

  function clamp(value, min, max) {
    return Math.max(min, Math.min(max, value));
  }

  function normalizeAngle(angle) {
    while (angle > Math.PI) angle -= Math.PI * 2;
    while (angle < -Math.PI) angle += Math.PI * 2;
    return angle;
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function mulberry32(seed) {
    return function random() {
      let t = (seed += 0x6d2b79f5);
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function scenarioConfig(scenario) {
    return SCENARIO[scenario] || SCENARIO.baseline;
  }

  function scenarioSeed(kind, difficulty, scenario, salt = 0) {
    const text = `${kind}:${difficulty}:${scenario}:${salt}`;
    let seed = 2166136261;
    for (let i = 0; i < text.length; i += 1) {
      seed ^= text.charCodeAt(i);
      seed = Math.imul(seed, 16777619);
    }
    return seed >>> 0;
  }

  function clearAroundPoint(grid, w, h, point, radius = 2) {
    for (let dy = -radius; dy <= radius; dy += 1) {
      for (let dx = -radius; dx <= radius; dx += 1) {
        setCell(grid, w, point.x + dx, point.y + dy, 0);
      }
    }
  }

  function gridRobotRadius(difficulty, scenario) {
    const base = {
      normal: 0.56,
      hard: 0.68,
      extreme: 0.82,
    }[difficulty] ?? 0.68;
    return base + scenarioConfig(scenario).generalization * 0.18;
  }

  function isGridFootprintBlocked(grid, w, h, x, y, radiusCells, extraBlocked) {
    const reach = Math.ceil(radiusCells);
    for (let yy = y - reach; yy <= y + reach; yy += 1) {
      for (let xx = x - reach; xx <= x + reach; xx += 1) {
        const centerDistance = Math.hypot(xx - x, yy - y);
        if (centerDistance > radiusCells + 0.52) continue;
        if (isGridBlocked(grid, w, h, xx, yy)) return true;
        if (extraBlocked && extraBlocked(xx, yy)) return true;
      }
    }
    return false;
  }

  function makeMovingObstacles(kind, difficulty, w, h, scenario, seed, options = {}) {
    const rng = mulberry32(scenarioSeed(`moving:${kind}`, difficulty, scenario, seed));
    const scenarioInfo = scenarioConfig(scenario);
    const speedScale = (DIFFICULTY[difficulty]?.multiplier ?? 1.2) * scenarioInfo.speedMultiplier;
    const count = options.count ?? (difficulty === "normal" ? 2 : difficulty === "hard" ? 3 : 4);
    const sizeScale = options.sizeScale ?? 1;
    const margin = options.margin ?? 5;
    const obstacles = [];
    for (let i = 0; i < count; i += 1) {
      const horizontal = i % 2 === 0;
      const ow = Math.max(2, Math.round((horizontal ? 4 : 3) * sizeScale));
      const oh = Math.max(2, Math.round((horizontal ? 3 : 4) * sizeScale));
      const band = (i + 1) / (count + 1);
      const x = clamp(Math.floor(w * band + (rng() - 0.5) * w * 0.18), margin, w - ow - margin);
      const y = clamp(Math.floor(h * (0.24 + (rng() * 0.52))), margin, h - oh - margin);
      obstacles.push({
        kind: "patrol",
        name: horizontal ? "横向移动障碍" : "纵向移动障碍",
        x,
        y,
        w: ow,
        h: oh,
        axis: horizontal ? "x" : "y",
        min: horizontal ? margin : margin,
        max: horizontal ? Math.max(margin + 1, w - ow - margin) : Math.max(margin + 1, h - oh - margin),
        dir: rng() > 0.5 ? 1 : -1,
        speed: (horizontal ? 3.2 : 2.6) * speedScale * (0.85 + rng() * 0.55),
      });
    }
    return obstacles;
  }

  function makePathMovingObstacles(kind, path, difficulty, scenario, seed, options = {}) {
    if (!path || path.length < 8) return [];
    const rng = mulberry32(scenarioSeed(`path-moving:${kind}`, difficulty, scenario, seed));
    const scenarioInfo = scenarioConfig(scenario);
    const speedScale = (DIFFICULTY[difficulty]?.multiplier ?? 1.2) * scenarioInfo.speedMultiplier;
    const count = options.count ?? (difficulty === "normal" ? 2 : difficulty === "hard" ? 3 : 4);
    const obstacles = [];
    for (let i = 0; i < count; i += 1) {
      const pathIndex = clamp(Math.floor(((i + 1) / (count + 1)) * path.length + (rng() - 0.5) * 8), 3, path.length - 4);
      const point = path[pathIndex];
      const previous = path[Math.max(0, pathIndex - 2)];
      const next = path[Math.min(path.length - 1, pathIndex + 2)];
      const horizontalSegment = Math.abs(next.x - previous.x) >= Math.abs(next.y - previous.y);
      const axis = horizontalSegment ? "x" : "y";
      const width = horizontalSegment ? 2.4 : 1.8;
      const height = horizontalSegment ? 1.8 : 2.4;
      const range = scenario === "generalization" ? 5 : scenario === "perturbed" ? 4 : 3;
      const x = point.x + 0.5 - width / 2;
      const y = point.y + 0.5 - height / 2;
      obstacles.push({
        kind: "patrol",
        name: "走廊移动障碍",
        x,
        y,
        w: width,
        h: height,
        axis,
        min: axis === "x" ? Math.max(1, x - range) : Math.max(1, y - range),
        max: axis === "x" ? x + range : y + range,
        dir: rng() > 0.5 ? 1 : -1,
        speed: (1.4 + rng() * 1.2) * speedScale,
      });
    }
    return obstacles;
  }

  function activeMovingObstacles(obstacles) {
    return obstacles.filter((obstacle) => obstacle.kind !== "gate" || obstacle.active);
  }

  function isMovingObstacleCell(obstacles, x, y) {
    return activeMovingObstacles(obstacles).some((obstacle) => (
      x + 1 > obstacle.x &&
      x < obstacle.x + obstacle.w &&
      y + 1 > obstacle.y &&
      y < obstacle.y + obstacle.h
    ));
  }

  function advanceMovingObstacles(obstacles, dt, speed, timeSec = 0) {
    const speedFactor = Math.max(0.3, speed / 5);
    const dtSec = (dt / 1000) * speedFactor;
    const nextTime = timeSec + dtSec;

    obstacles.forEach((obstacle) => {
      if (obstacle.kind === "patrol") {
        const next = obstacle[obstacle.axis] + obstacle.dir * obstacle.speed * dtSec;
        if (next < obstacle.min) {
          obstacle[obstacle.axis] = obstacle.min + (obstacle.min - next);
          obstacle.dir = 1;
        } else if (next > obstacle.max) {
          obstacle[obstacle.axis] = obstacle.max - (next - obstacle.max);
          obstacle.dir = -1;
        } else {
          obstacle[obstacle.axis] = next;
        }
      }

      if (obstacle.kind === "rect") {
        let remaining = obstacle.speed * dtSec;
        while (remaining > 0) {
          const target = obstacle.route[obstacle.target];
          const dx = target.x - obstacle.x;
          const dy = target.y - obstacle.y;
          const dist = Math.hypot(dx, dy);
          if (dist < 0.001) {
            obstacle.target = (obstacle.target + 1) % obstacle.route.length;
            continue;
          }
          if (dist <= remaining) {
            obstacle.x = target.x;
            obstacle.y = target.y;
            obstacle.target = (obstacle.target + 1) % obstacle.route.length;
            remaining -= dist;
          } else {
            obstacle.x += (dx / dist) * remaining;
            obstacle.y += (dy / dist) * remaining;
            remaining = 0;
          }
        }
      }

      if (obstacle.kind === "diagonal") {
        const nextX = obstacle.x + obstacle.dirX * obstacle.speedX * dtSec;
        const nextY = obstacle.y + obstacle.dirY * obstacle.speedY * dtSec;
        if (nextX < obstacle.minX || nextX > obstacle.maxX) obstacle.dirX *= -1;
        if (nextY < obstacle.minY || nextY > obstacle.maxY) obstacle.dirY *= -1;
        obstacle.x = clamp(nextX, obstacle.minX, obstacle.maxX);
        obstacle.y = clamp(nextY, obstacle.minY, obstacle.maxY);
      }

      if (obstacle.kind === "gate") {
        const cycle = (nextTime + obstacle.phase) % obstacle.period;
        obstacle.active = cycle < obstacle.period * obstacle.duty;
      }
    });

    return nextTime;
  }

  function movingObstacleMaxSpeed(obstacles) {
    return obstacles.reduce((max, obstacle) => Math.max(max, obstacle.speed || obstacle.speedX || 0), 0);
  }

  function gridPathProgressPoint(path, progress) {
    if (!path || path.length === 0) return null;
    if (path.length === 1) return { x: path[0].x + 0.5, y: path[0].y + 0.5 };
    const capped = clamp(progress, 0, path.length - 1);
    const indexValue = Math.floor(capped);
    const nextIndex = Math.min(path.length - 1, indexValue + 1);
    const t = capped - indexValue;
    const a = path[indexValue];
    const b = path[nextIndex];
    return {
      x: lerp(a.x + 0.5, b.x + 0.5, t),
      y: lerp(a.y + 0.5, b.y + 0.5, t),
    };
  }

  function applyGridScenario(grid, w, h, start, goal, difficulty, scenario, kind, seed = 0) {
    const config = scenarioConfig(scenario);
    const rng = mulberry32(scenarioSeed(kind, difficulty, scenario, seed));
    const baseBlocks = scenario === "baseline" ? (difficulty === "normal" ? 2 : difficulty === "hard" ? 3 : 4) : config.randomBlocks;
    const blockCount = baseBlocks + (scenario === "baseline" ? 0 : difficulty === "extreme" ? 6 : difficulty === "hard" ? 3 : 0);
    let extraBlocks = 0;

    for (let i = 0; i < blockCount; i += 1) {
      const bw = (scenario === "baseline" ? 2 : 3) + Math.floor(rng() * (difficulty === "extreme" ? 5 : 4));
      const bh = (scenario === "baseline" ? 2 : 2) + Math.floor(rng() * (difficulty === "extreme" ? 4 : 3));
      const x = 3 + Math.floor(rng() * Math.max(1, w - bw - 6));
      const y = 3 + Math.floor(rng() * Math.max(1, h - bh - 6));
      const nearStart = Math.abs(x - start.x) < 8 && Math.abs(y - start.y) < 8;
      const nearGoal = Math.abs(x - goal.x) < 8 && Math.abs(y - goal.y) < 8;
      if (nearStart || nearGoal) continue;
      fillRectCells(grid, w, h, x, y, bw, bh, 1);
      extraBlocks += 1;
    }

    let shiftedWalls = 0;
    if (scenario === "generalization") {
      const lanes = difficulty === "normal" ? 2 : difficulty === "hard" ? 3 : 4;
      for (let i = 0; i < lanes; i += 1) {
        const x = Math.floor(((i + 1) * w) / (lanes + 1)) + (i % 2 === 0 ? 2 : -3);
        const gapA = 6 + ((i * 13 + config.obstacleShift) % Math.max(8, h - 18));
        const gapB = Math.min(h - 5, gapA + 7);
        for (let y = 3; y < h - 3; y += 1) {
          const open = y >= gapA && y <= gapB;
          if (!open && (y + i) % 5 !== 0) setCell(grid, w, x, y, 1);
        }
        shiftedWalls += 1;
      }
    }

    clearAroundPoint(grid, w, h, start, 3);
    clearAroundPoint(grid, w, h, goal, 3);
    return { extraBlocks, shiftedWalls };
  }

  function makeAiGridRollout(grid, w, h, start, goal, baseRollout, difficulty, scenario, kind, extraBlocked) {
    if (scenario === "baseline" && baseRollout) return baseRollout;
    const result = gridSearch({ grid, w, h, start, goal, heuristic: manhattan, extraBlocked });
    const config = scenarioConfig(scenario);
    const penalty = scenario === "generalization" ? 0.82 : 0.92;
    return {
      path: result.path,
      visited: [],
      metrics: {
        success: result.success,
        steps: result.pathLength,
        policy_ms: (baseRollout?.metrics?.policy_ms || 1.8) + config.latencyMs / 1000,
        searched_nodes: Math.floor(result.visitedOrder.length * (1 - penalty)),
        generalization_gap: scenario === "baseline" ? 0 : scenario === "perturbed" ? 0.08 : 0.18,
        robustness_score: Math.max(0.35, penalty - config.sensorNoise * 0.2),
      },
      training: baseRollout?.training || {},
    };
  }

  function makeAiCarRollout(grid, w, h, start, goal, baseRollout, difficulty, scenario) {
    if (scenario === "baseline" && baseRollout) return baseRollout;
    const result = gridSearch({ grid, w, h, start, goal, heuristic: manhattan });
    const trail = [];
    const path = result.path.length ? result.path : [start];
    for (let i = 1; i < path.length; i += 1) {
      const a = cellCenter(path[i - 1]);
      const b = cellCenter(path[i]);
      const segments = Math.max(2, Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) * 3));
      for (let j = 0; j < segments; j += 1) {
        const t = j / segments;
        trail.push({ x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t) });
      }
    }
    if (path.length) trail.push(cellCenter(path[path.length - 1]));
    const config = scenarioConfig(scenario);
    const gap = scenario === "perturbed" ? 0.1 : 0.2;
    return {
      path,
      trail,
      visited: [],
      metrics: {
        success: result.success,
        collisions: 0,
        smoothness: Math.max(0.62, (baseRollout?.metrics?.smoothness ?? 0.84) - gap),
        policy_ms: (baseRollout?.metrics?.policy_ms || 2.2) + config.latencyMs / 900,
        searched_nodes: Math.floor(result.visitedOrder.length * (scenario === "generalization" ? 0.18 : 0.1)),
        generalization_gap: scenario === "perturbed" ? 0.1 : 0.22,
      },
      training: baseRollout?.training || {},
    };
  }

  function percent(value) {
    return `${formatNumber(clamp(value, 0, 1) * 100, 0)}%`;
  }

  function splitBoxes(width, height, count) {
    const gap = 12;
    const margin = 14;
    if (count === 2 && width < 760) {
      const usableH = height - margin * 2 - gap;
      const boxH = usableH / 2;
      return [
        { x: margin, y: margin, w: width - margin * 2, h: boxH },
        { x: margin, y: margin + boxH + gap, w: width - margin * 2, h: boxH },
      ];
    }
    const usableW = width - margin * 2 - gap * (count - 1);
    const boxW = usableW / count;
    return Array.from({ length: count }, (_, i) => ({
      x: margin + i * (boxW + gap),
      y: margin,
      w: boxW,
      h: height - margin * 2,
    }));
  }

  function drawBox(box, title, subtitle) {
    ctx.fillStyle = COLOR.panel;
    ctx.strokeStyle = COLOR.panelLine;
    ctx.lineWidth = 1;
    roundedRectPath(box.x, box.y, box.w, box.h, 8);
    ctx.fill();
    ctx.stroke();

    ctx.fillStyle = COLOR.text;
    ctx.font = "600 15px Inter, sans-serif";
    ctx.fillText(title, box.x + 14, box.y + 22);
    if (subtitle) {
      ctx.fillStyle = COLOR.muted;
      ctx.font = "12px Inter, sans-serif";
      ctx.fillText(subtitle, box.x + 14, box.y + 42);
    }
  }

  function getGridGeometry(box, w, h, titleHeight = 50) {
    const pad = 12;
    const availableW = box.w - pad * 2;
    const availableH = box.h - pad * 2 - titleHeight;
    const cell = Math.max(2, Math.floor(Math.min(availableW / w, availableH / h)));
    const gridW = cell * w;
    const gridH = cell * h;
    return {
      x: box.x + pad + (availableW - gridW) / 2,
      y: box.y + titleHeight + pad + (availableH - gridH) / 2,
      cell,
      gridW,
      gridH,
      cols: w,
      rows: h,
    };
  }

  function drawGridBase(grid, w, h, geom) {
    ctx.fillStyle = "#0b0f15";
    ctx.fillRect(geom.x, geom.y, geom.gridW, geom.gridH);

    ctx.fillStyle = COLOR.obstacle;
    for (let y = 0; y < h; y += 1) {
      for (let x = 0; x < w; x += 1) {
        if (grid[idx(x, y, w)] === 1) {
          ctx.fillRect(geom.x + x * geom.cell, geom.y + y * geom.cell, geom.cell, geom.cell);
        }
      }
    }

    if (geom.cell >= 7) {
      ctx.strokeStyle = COLOR.gridLine;
      ctx.lineWidth = 1;
      for (let x = 0; x <= w; x += 1) {
        const px = geom.x + x * geom.cell;
        ctx.beginPath();
        ctx.moveTo(px, geom.y);
        ctx.lineTo(px, geom.y + geom.gridH);
        ctx.stroke();
      }
      for (let y = 0; y <= h; y += 1) {
        const py = geom.y + y * geom.cell;
        ctx.beginPath();
        ctx.moveTo(geom.x, py);
        ctx.lineTo(geom.x + geom.gridW, py);
        ctx.stroke();
      }
    }

    ctx.strokeStyle = "#566276";
    ctx.lineWidth = 1;
    ctx.strokeRect(geom.x, geom.y, geom.gridW, geom.gridH);
  }

  function drawGridMovingRoutes(obstacles, geom) {
    if (!obstacles || obstacles.length === 0) return;
    ctx.save();
    ctx.strokeStyle = "rgba(247, 152, 36, 0.34)";
    ctx.lineWidth = 2;
    ctx.setLineDash([6, 5]);
    obstacles.forEach((obstacle) => {
      if (obstacle.kind === "patrol") {
        const cx = obstacle.x + obstacle.w / 2;
        const cy = obstacle.y + obstacle.h / 2;
        const xA = obstacle.axis === "x" ? obstacle.min + obstacle.w / 2 : cx;
        const xB = obstacle.axis === "x" ? obstacle.max + obstacle.w / 2 : cx;
        const yA = obstacle.axis === "y" ? obstacle.min + obstacle.h / 2 : cy;
        const yB = obstacle.axis === "y" ? obstacle.max + obstacle.h / 2 : cy;
        ctx.beginPath();
        ctx.moveTo(geom.x + xA * geom.cell, geom.y + yA * geom.cell);
        ctx.lineTo(geom.x + xB * geom.cell, geom.y + yB * geom.cell);
        ctx.stroke();
      }
      if (obstacle.kind === "rect") {
        ctx.beginPath();
        obstacle.route.forEach((point, indexValue) => {
          const x = geom.x + (point.x + obstacle.w / 2) * geom.cell;
          const y = geom.y + (point.y + obstacle.h / 2) * geom.cell;
          if (indexValue === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        });
        const first = obstacle.route[0];
        ctx.lineTo(geom.x + (first.x + obstacle.w / 2) * geom.cell, geom.y + (first.y + obstacle.h / 2) * geom.cell);
        ctx.stroke();
      }
      if (obstacle.kind === "diagonal") {
        ctx.beginPath();
        ctx.moveTo(geom.x + (obstacle.minX + obstacle.w / 2) * geom.cell, geom.y + (obstacle.minY + obstacle.h / 2) * geom.cell);
        ctx.lineTo(geom.x + (obstacle.maxX + obstacle.w / 2) * geom.cell, geom.y + (obstacle.maxY + obstacle.h / 2) * geom.cell);
        ctx.stroke();
      }
    });
    ctx.restore();
  }

  function drawGridMovingObstacles(obstacles, geom) {
    if (!obstacles || obstacles.length === 0) return;
    obstacles.forEach((obstacle) => {
      ctx.save();
      ctx.globalAlpha = obstacle.kind === "gate" && !obstacle.active ? 0.22 : 0.94;
      ctx.fillStyle = COLOR.dynamic;
      ctx.strokeStyle = COLOR.dynamic;
      ctx.lineWidth = 2;
      const x = geom.x + obstacle.x * geom.cell;
      const y = geom.y + obstacle.y * geom.cell;
      const w = obstacle.w * geom.cell;
      const h = obstacle.h * geom.cell;
      if (obstacle.kind === "gate" && !obstacle.active) {
        ctx.strokeRect(x, y, w, h);
      } else {
        ctx.fillRect(x, y, w, h);
      }
      ctx.restore();
    });
  }

  function drawVisited(cells, geom, color, limit, alpha = 0.28) {
    const capped = Math.min(cells.length, Math.floor(limit));
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    for (let i = 0; i < capped; i += 1) {
      const cell = cells[i];
      ctx.fillRect(
        geom.x + cell.x * geom.cell + 1,
        geom.y + cell.y * geom.cell + 1,
        Math.max(1, geom.cell - 2),
        Math.max(1, geom.cell - 2),
      );
    }
    ctx.restore();
  }

  function drawGridPath(path, geom, color, width = 3, alpha = 1) {
    if (!path || path.length < 2) return;
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    path.forEach((point, indexValue) => {
      const px = geom.x + point.x * geom.cell + geom.cell / 2;
      const py = geom.y + point.y * geom.cell + geom.cell / 2;
      if (indexValue === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
    ctx.restore();
  }

  function drawMarker(point, geom, color, label) {
    const cx = geom.x + point.x * geom.cell + geom.cell / 2;
    const cy = geom.y + point.y * geom.cell + geom.cell / 2;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(4, geom.cell * 0.42), 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#091016";
    ctx.font = "700 10px Inter, sans-serif";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, cx, cy + 0.5);
    ctx.textAlign = "start";
    ctx.textBaseline = "alphabetic";
  }

  function drawGridRobotFootprint(point, geom, radiusCells, color, label = "R") {
    if (!point) return;
    const cx = geom.x + point.x * geom.cell;
    const cy = geom.y + point.y * geom.cell;
    const radius = Math.max(geom.cell * 0.52, radiusCells * geom.cell);
    ctx.save();
    ctx.globalAlpha = 0.16;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(cx, cy, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 0.88;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.4;
    ctx.stroke();
    ctx.globalAlpha = 1;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(cx, cy, Math.max(3.5, geom.cell * 0.28), 0, Math.PI * 2);
    ctx.fill();
    if (label) {
      ctx.fillStyle = "#091016";
      ctx.font = "700 10px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, cx, cy + 0.5);
      ctx.textAlign = "start";
      ctx.textBaseline = "alphabetic";
    }
    ctx.restore();
  }

  function drawMetricText(box, lines) {
    ctx.font = "12px Inter, sans-serif";
    lines.forEach((line, i) => {
      ctx.fillStyle = i === 0 ? COLOR.text : COLOR.muted;
      ctx.fillText(line, box.x + 14, box.y + box.h - 18 - (lines.length - 1 - i) * 17);
    });
  }

  function drawTrainingCurve(box, rollout) {
    const curve = rollout?.training?.lossCurve || [];
    if (curve.length < 2 || box.w < 300) return;
    const chartW = Math.min(128, box.w * 0.26);
    const chartH = 34;
    const x = box.x + box.w - chartW - 14;
    const y = box.y + 13;
    const min = Math.min(...curve);
    const max = Math.max(...curve);
    const range = Math.max(0.001, max - min);

    ctx.save();
    ctx.globalAlpha = 0.96;
    ctx.strokeStyle = "rgba(255,255,255,0.16)";
    ctx.strokeRect(x, y, chartW, chartH);
    ctx.strokeStyle = COLOR.actual;
    ctx.lineWidth = 2;
    ctx.beginPath();
    curve.forEach((value, indexValue) => {
      const px = x + (indexValue / (curve.length - 1)) * chartW;
      const py = y + chartH - ((value - min) / range) * chartH;
      if (indexValue === 0) ctx.moveTo(px, py);
      else ctx.lineTo(px, py);
    });
    ctx.stroke();
    ctx.fillStyle = COLOR.muted;
    ctx.font = "10px Inter, sans-serif";
    ctx.fillText("loss", x, y + chartH + 12);
    ctx.restore();
  }

  function drawGridAiPanel(box, title, subtitle, grid, w, h, start, goal, rollout, progress, options = {}) {
    drawBox(box, title, subtitle);
    drawTrainingCurve(box, rollout);
    const geom = getGridGeometry(box, w, h, 54);
    drawGridBase(grid, w, h, geom);
    if (options.afterBase) options.afterBase(geom);
    const visited = rollout?.visited || [];
    if (visited.length) drawVisited(visited, geom, COLOR.actual, progress * 3, 0.12);
    const path = rollout?.path || [start];
    const shownPath = progressSlice(path, progress);
    drawGridPath(shownPath, geom, COLOR.actual, 3.2, 1);

    if (options.waitSteps && path.length) {
      ctx.save();
      ctx.fillStyle = COLOR.dynamic;
      options.waitSteps.forEach((stepIndex) => {
        if (stepIndex > progress || !path[stepIndex]) return;
        const point = path[stepIndex];
        ctx.fillRect(
          geom.x + point.x * geom.cell + geom.cell * 0.18,
          geom.y + point.y * geom.cell + geom.cell * 0.18,
          geom.cell * 0.64,
          geom.cell * 0.64,
        );
      });
      ctx.restore();
    }

    const current = shownPath[shownPath.length - 1] || start;
    const currentFootprint = gridPathProgressPoint(path, progress) || { x: start.x + 0.5, y: start.y + 0.5 };
    if (options.robotRadiusCells) {
      drawGridRobotFootprint(currentFootprint, geom, options.robotRadiusCells, COLOR.actual, options.robotLabel || "A");
    }
    drawMarker(start, geom, COLOR.start, "S");
    drawMarker(goal, geom, COLOR.goal, "G");
    if (!options.robotRadiusCells) drawMarker(current, geom, COLOR.actual, "A");

    const metrics = rollout?.metrics || {};
    const steps = metrics.steps ?? Math.max(0, path.length - 1);
    const searched = metrics.searched_nodes ?? metrics.replans ?? 0;
    drawMetricText(box, [
      `AI: ${metrics.success === false ? "未到达" : "策略轨迹"} | ${checkpointLabel(rollout)}`,
      `动作 ${Math.min(Math.floor(progress), path.length)}/${path.length} | 搜索 ${searched} | 推理 ${formatNumber(metrics.policy_ms, 1)}ms`,
      `路径步数 ${steps}`,
    ]);
  }

  function roundedRectPath(x, y, w, h, radius) {
    const r = Math.min(radius, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.lineTo(x + w - r, y);
    ctx.quadraticCurveTo(x + w, y, x + w, y + r);
    ctx.lineTo(x + w, y + h - r);
    ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    ctx.lineTo(x + r, y + h);
    ctx.quadraticCurveTo(x, y + h, x, y + h - r);
    ctx.lineTo(x, y + r);
    ctx.quadraticCurveTo(x, y, x + r, y);
    ctx.closePath();
  }

  function makeSearchMap(difficulty = "hard", scenario = "baseline", seed = 0) {
    const config = {
      normal: { w: 74, h: 46, wallCount: 4, gapSize: 8, traps: 6 },
      hard: { w: 92, h: 56, wallCount: 6, gapSize: 7, traps: 12 },
      extreme: { w: 108, h: 64, wallCount: 8, gapSize: 6, traps: 18 },
    }[difficulty] ?? { w: 92, h: 56, wallCount: 6, gapSize: 7, traps: 12 };
    const { w, h, wallCount, gapSize, traps } = config;
    const grid = createGrid(w, h, 0);
    for (let x = 0; x < w; x += 1) {
      setCell(grid, w, x, 0, 1);
      setCell(grid, w, x, h - 1, 1);
    }
    for (let y = 0; y < h; y += 1) {
      setCell(grid, w, 0, y, 1);
      setCell(grid, w, w - 1, y, 1);
    }

    for (let i = 1; i <= wallCount; i += 1) {
      const x = Math.floor((w * i) / (wallCount + 1));
      const gapMid = i % 2 === 1 ? 8 + (i % 3) * 4 : h - 10 - (i % 3) * 5;
      const gapA = clamp(Math.floor(gapMid - gapSize / 2), 3, h - gapSize - 4);
      const gapB = gapA + gapSize;
      for (let y = 2; y < h - 2; y += 1) {
        if (y < gapA || y > gapB) setCell(grid, w, x, y, 1);
      }
      // Dead-end branches make uninformed expansion visibly more expensive.
      const branchY = i % 2 === 1 ? h - 14 - (i % 4) * 3 : 10 + (i % 4) * 3;
      const left = Math.max(2, x - Math.floor(w / (wallCount + 1)) + 3);
      const right = Math.min(w - 3, x - 2);
      for (let bx = left; bx <= right; bx += 1) {
        setCell(grid, w, bx, branchY, 1);
      }
    }

    for (let i = 0; i < traps; i += 1) {
      const blockW = 4 + (i % 4);
      const blockH = 3 + (i % 3);
      const x = 6 + ((i * 17) % Math.max(10, w - 18));
      const y = 8 + ((i * 11) % Math.max(10, h - 18));
      if (x < 8 && y < 8) continue;
      if (x > w - 16 && y > h - 14) continue;
      fillRectCells(grid, w, h, x, y, blockW, blockH, 1);
    }

    const start = { x: 3, y: 4 };
    const goal = { x: w - 5, y: h - 5 };
    for (let dy = -1; dy <= 1; dy += 1) {
      for (let dx = -1; dx <= 1; dx += 1) {
        setCell(grid, w, start.x + dx, start.y + dy, 0);
        setCell(grid, w, goal.x + dx, goal.y + dy, 0);
      }
    }
    const scenarioStats = applyGridScenario(grid, w, h, start, goal, difficulty, scenario, "search", seed);
    return { grid, w, h, start, goal, scenarioStats };
  }

  function makeDynamicMap(difficulty = "hard", scenario = "baseline", seed = 0) {
    const config = {
      normal: { w: 70, h: 42 },
      hard: { w: 86, h: 52 },
      extreme: { w: 100, h: 60 },
    }[difficulty] ?? { w: 86, h: 52 };
    const { w, h } = config;
    const grid = createGrid(w, h, 0);
    for (let x = 0; x < w; x += 1) {
      setCell(grid, w, x, 0, 1);
      setCell(grid, w, x, h - 1, 1);
    }
    for (let y = 0; y < h; y += 1) {
      setCell(grid, w, 0, y, 1);
      setCell(grid, w, w - 1, y, 1);
    }

    const addVerticalWall = (x, gaps) => {
      for (let y = 2; y < h - 2; y += 1) {
        const open = gaps.some(([a, b]) => y >= a && y <= b);
        if (!open) setCell(grid, w, x, y, 1);
      }
    };
    const addHorizontalWall = (y, gaps) => {
      for (let x = 2; x < w - 2; x += 1) {
        const open = gaps.some(([a, b]) => x >= a && x <= b);
        if (!open) setCell(grid, w, x, y, 1);
      }
    };

    const midY = Math.floor(h / 2);
    const x1 = Math.floor(w * 0.18);
    const x2 = Math.floor(w * 0.36);
    const x3 = Math.floor(w * 0.55);
    const x4 = Math.floor(w * 0.73);

    addVerticalWall(x1, [
      [midY - 4, midY + 4],
      [5, 8],
    ]);
    addVerticalWall(x2, [
      [7, 13],
      [h - 13, h - 8],
    ]);
    addVerticalWall(x3, [
      [midY - 5, midY + 5],
      [h - 10, h - 7],
    ]);
    addVerticalWall(x4, [
      [9, 15],
      [midY + 8, midY + 13],
    ]);

    addHorizontalWall(Math.floor(h * 0.26), [
      [x1 - 4, x1 + 5],
      [x3 - 6, x3 + 6],
      [w - 15, w - 8],
    ]);
    addHorizontalWall(Math.floor(h * 0.72), [
      [5, 13],
      [x2 - 6, x2 + 6],
      [x4 - 5, x4 + 5],
    ]);

    if (difficulty !== "normal") {
      fillRectCells(grid, w, h, Math.floor(w * 0.23), Math.floor(h * 0.38), 8, 4, 1);
      fillRectCells(grid, w, h, Math.floor(w * 0.45), Math.floor(h * 0.55), 9, 4, 1);
      fillRectCells(grid, w, h, Math.floor(w * 0.66), Math.floor(h * 0.34), 7, 5, 1);
    }
    if (difficulty === "extreme") {
      fillRectCells(grid, w, h, Math.floor(w * 0.11), Math.floor(h * 0.57), 10, 3, 1);
      fillRectCells(grid, w, h, Math.floor(w * 0.81), Math.floor(h * 0.47), 8, 7, 1);
    }

    const start = { x: 4, y: midY };
    const goal = { x: w - 5, y: midY };
    fillRectCells(grid, w, h, start.x - 1, start.y - 1, 3, 3, 0);
    fillRectCells(grid, w, h, goal.x - 1, goal.y - 1, 3, 3, 0);
    const scenarioStats = applyGridScenario(grid, w, h, start, goal, difficulty, scenario, "dynamic", seed);
    return { grid, w, h, start, goal, scenarioStats };
  }

  function makeCarMap(difficulty = "hard", scenario = "baseline", seed = 0) {
    const config = {
      normal: {
        w: 46,
        h: 34,
        width: 5,
        points: [
          { x: 3, y: 25 },
          { x: 14, y: 25 },
          { x: 14, y: 9 },
          { x: 34, y: 9 },
          { x: 34, y: 26 },
          { x: 42, y: 26 },
        ],
      },
      hard: {
        w: 58,
        h: 40,
        width: 3,
        points: [
          { x: 3, y: 34 },
          { x: 14, y: 34 },
          { x: 14, y: 7 },
          { x: 26, y: 7 },
          { x: 26, y: 32 },
          { x: 38, y: 32 },
          { x: 38, y: 9 },
          { x: 52, y: 9 },
          { x: 52, y: 35 },
          { x: 55, y: 35 },
        ],
      },
      extreme: {
        w: 66,
        h: 44,
        width: 3,
        points: [
          { x: 3, y: 38 },
          { x: 12, y: 38 },
          { x: 12, y: 6 },
          { x: 22, y: 6 },
          { x: 22, y: 36 },
          { x: 32, y: 36 },
          { x: 32, y: 8 },
          { x: 44, y: 8 },
          { x: 44, y: 34 },
          { x: 55, y: 34 },
          { x: 55, y: 11 },
          { x: 62, y: 11 },
        ],
      },
    }[difficulty] ?? {
      w: 58,
      h: 40,
      width: 3,
      points: [
        { x: 3, y: 34 },
        { x: 14, y: 34 },
        { x: 14, y: 7 },
        { x: 26, y: 7 },
        { x: 26, y: 32 },
        { x: 38, y: 32 },
        { x: 38, y: 9 },
        { x: 52, y: 9 },
        { x: 52, y: 35 },
        { x: 55, y: 35 },
      ],
    };
    const { w, h } = config;
    const scenarioInfo = scenarioConfig(scenario);
    const widthPenalty = scenario === "generalization" ? 1 : 0;
    const width = Math.max(2, config.width - widthPenalty);
    const rng = mulberry32(scenarioSeed("car-corridor", difficulty, scenario, seed));
    const jitter = scenario === "baseline" ? 1 : scenario === "perturbed" ? 2 : 3;
    const xAdjustments = new Map();
    const yAdjustments = new Map();
    const adjustedAxisValue = (map, value, min, max) => {
      if (!map.has(value)) {
        const delta = Math.round((rng() * 2 - 1) * jitter);
        map.set(value, clamp(value + delta, min, max));
      }
      return map.get(value);
    };
    const points = config.points.map((point) => ({
      x: adjustedAxisValue(xAdjustments, point.x, 3, w - 4),
      y: adjustedAxisValue(yAdjustments, point.y, 3, h - 4),
    }));
    const grid = createGrid(w, h, 1);
    const half = Math.floor(width / 2);
    for (let i = 1; i < points.length; i += 1) {
      const a = points[i - 1];
      const b = points[i];
      if (a.x === b.x) {
        const y = Math.min(a.y, b.y) - half;
        const length = Math.abs(a.y - b.y) + width;
        fillRectCells(grid, w, h, a.x - half, y, width, length, 0);
      } else {
        const x = Math.min(a.x, b.x) - half;
        const length = Math.abs(a.x - b.x) + width;
        fillRectCells(grid, w, h, x, a.y - half, length, width, 0);
      }
    }
    points.forEach((point) => fillRectCells(grid, w, h, point.x - half, point.y - half, width, width, 0));
    const scenarioStats = {
      corridorShift: scenarioInfo.obstacleShift,
      corridorWidthChange: config.width - width,
      carScale: scenarioInfo.carScale,
    };
    return { grid, w, h, start: points[0], goal: points[points.length - 1], corridorWidth: width, scenarioStats };
  }

  class SearchComparisonDemo {
    constructor(difficulty = "hard", scenario = "baseline") {
      this.demoId = "search";
      this.difficulty = difficulty;
      this.scenario = scenario;
      this.title = "Dijkstra vs AI 训练策略";
      this.claim = "Dijkstra 没有目标方向感，会从起点向外扩散。AI 侧把专家路径训练成策略，演示时直接给动作，几乎不做临场搜索。";
      this.mapSerial = 0;
      this.mapSeed = 0;
      this.reset(true);
    }

    reset(forceNewMap = false) {
      ensureMapIdentity(this, forceNewMap);
      this.scenarioInfo = scenarioConfig(this.scenario);
      this.robotRadius = gridRobotRadius(this.difficulty, this.scenario);
      this.trainingState = getRuntimeTraining(this.demoId, this.difficulty, this.scenario);
      let selected = null;
      for (let attempt = 0; attempt < 35; attempt += 1) {
        const map = makeSearchMap(this.difficulty, this.scenario, this.mapSeed + attempt * 7919);
        const dijkstra = gridSearch({
          grid: map.grid,
          w: map.w,
          h: map.h,
          start: map.start,
          goal: map.goal,
          heuristic: () => 0,
        });
        const astar = gridSearch({
          grid: map.grid,
          w: map.w,
          h: map.h,
          start: map.start,
          goal: map.goal,
          heuristic: manhattan,
        });
        if (dijkstra.success && astar.success) {
          selected = { map, dijkstra, astar };
          break;
        }
      }
      if (!selected) {
        const map = makeSearchMap(this.difficulty, "baseline", this.mapSeed);
        selected = {
          map,
          dijkstra: gridSearch({ grid: map.grid, w: map.w, h: map.h, start: map.start, goal: map.goal, heuristic: () => 0 }),
          astar: gridSearch({ grid: map.grid, w: map.w, h: map.h, start: map.start, goal: map.goal, heuristic: manhattan }),
        };
      }
      Object.assign(this, selected.map);
      this.dijkstra = selected.dijkstra;
      this.astar = selected.astar;
      this.movingObstacles = makeMovingObstacles("search", this.difficulty, this.w, this.h, this.scenario, this.mapSeed, { count: this.difficulty === "normal" ? 2 : 3 });
      this.timeSec = 0;
      this.progress = 0;
      this.pathProgress = 0;
      this.aiProgress = 0;
      this.aiRollout = makeAiGridRollout(
        this.grid,
        this.w,
        this.h,
        this.start,
        this.goal,
        getAiRollout("dijkstra", this.difficulty),
        this.difficulty,
        this.scenario,
        "search",
      );
      this.traditionalRunning = false;
      this.aiRunning = false;
      this.traditionalDone = false;
      this.aiDone = false;
      this.running = false;
    }

    run() {
      this.reset(true);
      this.trainingState = trainRuntimeAi(this.demoId, this.difficulty, this.scenario);
      this.traditionalRunning = true;
      this.aiRunning = true;
      this.traditionalDone = false;
      this.aiDone = false;
      this.running = true;
    }

    pause() {
      this.traditionalRunning = false;
      this.aiRunning = false;
      this.running = false;
    }

    step() {
      this.timeSec = advanceMovingObstacles(this.movingObstacles, 180, 5, this.timeSec);
      if (!this.traditionalDone) {
        this.progress += 80;
        if (this.progress >= this.dijkstra.visitedOrder.length) this.pathProgress += 6;
        if (this.pathProgress > Math.max(0, this.dijkstra.path.length - 1) + 3) {
          this.traditionalDone = true;
          this.traditionalRunning = false;
        }
      }
      if (!this.aiDone) {
        this.aiProgress += 12;
        if (this.aiProgress > (this.aiRollout?.path?.length || 0) + 4) {
          this.aiDone = true;
          this.aiRunning = false;
        }
      }
      this.running = this.traditionalRunning || this.aiRunning;
    }

    update(dt, speed) {
      if (!this.traditionalRunning && !this.aiRunning) return;
      this.timeSec = advanceMovingObstacles(this.movingObstacles, dt, speed, this.timeSec);
      if (this.traditionalRunning && !this.traditionalDone) {
        if (this.progress < this.dijkstra.visitedOrder.length) {
          this.progress += dt * (0.18 + speed * 0.075);
        } else {
          this.pathProgress += dt * (0.012 + speed * 0.009);
        }
        if (this.pathProgress > Math.max(0, this.dijkstra.path.length - 1) + 3) {
          this.traditionalDone = true;
          this.traditionalRunning = false;
        }
      }
      if (this.aiRunning && !this.aiDone) {
        this.aiProgress += dt * (0.018 + speed * 0.012);
        if (this.aiProgress > (this.aiRollout?.path?.length || 0) + 10) {
          this.aiDone = true;
          this.aiRunning = false;
        }
      }
      this.running = this.traditionalRunning || this.aiRunning;
    }

    render(width, height) {
      const boxes = splitBoxes(width, height, 2);
      this.renderOne(boxes[0], "Dijkstra", this.dijkstra, COLOR.visited);
      drawGridAiPanel(
        boxes[1],
        "AI 训练策略",
        "右侧显示训练后的动作轨迹，不展开全图搜索",
        this.grid,
        this.w,
        this.h,
        this.start,
        this.goal,
        this.aiRollout,
        this.aiProgress,
        {
          robotRadiusCells: this.robotRadius,
          robotLabel: "A",
          afterBase: (geom) => {
            drawGridMovingRoutes(this.movingObstacles, geom);
            drawGridMovingObstacles(this.movingObstacles, geom);
          },
        },
      );
    }

    renderOne(box, label, result, visitedColor) {
      drawBox(box, label, "没有启发方向，按代价一圈圈扩散");
      const geom = getGridGeometry(box, this.w, this.h, 54);
      drawGridBase(this.grid, this.w, this.h, geom);
      drawGridMovingRoutes(this.movingObstacles, geom);
      drawVisited(result.visitedOrder, geom, visitedColor, this.progress, 0.28);
      if (this.progress >= result.visitedOrder.length) {
        drawGridPath(progressSlice(result.path, this.pathProgress + 1), geom, COLOR.path, 3.2, 1);
        drawGridRobotFootprint(gridPathProgressPoint(result.path, this.pathProgress), geom, this.robotRadius, COLOR.path, "R");
      } else {
        const latest = result.visitedOrder[Math.max(0, Math.min(result.visitedOrder.length - 1, Math.floor(this.progress) - 1))];
        if (latest) drawGridRobotFootprint({ x: latest.x + 0.5, y: latest.y + 0.5 }, geom, this.robotRadius, visitedColor, "");
      }
      drawGridMovingObstacles(this.movingObstacles, geom);
      drawMarker(this.start, geom, COLOR.start, "S");
      drawMarker(this.goal, geom, COLOR.goal, "G");
      const searched = Math.min(result.visitedOrder.length, Math.floor(this.progress));
      drawMetricText(box, [
        `${label}: ${result.success ? "找到路径" : "失败"}`,
        `搜索格子 ${searched}/${result.visitedOrder.length} | 路径 ${result.pathLength} | 计算 ${formatNumber(result.timeMs, 2)}ms`,
      ]);
    }

    metrics() {
      const dVisited = Math.min(this.dijkstra.visitedOrder.length, Math.floor(this.progress));
      const aiMetrics = this.aiRollout?.metrics || {};
      const reduction = 1 - (aiMetrics.searched_nodes ?? 0) / Math.max(1, this.dijkstra.visitedOrder.length);
      return [
        ["实验强度", DIFFICULTY[this.difficulty]?.label ?? this.difficulty],
        ["环境模式", SCENARIO[this.scenario]?.label ?? this.scenario],
        ["地图编号", `#${this.mapSerial}`],
        ["额外扰动块", this.scenarioStats?.extraBlocks ?? 0],
        ["移动障碍数", this.movingObstacles.length],
        ["机器人半径", `${formatNumber(this.robotRadius, 2)} 格`],
        ["机器人位置", `${formatNumber(this.pathProgress, 1)}/${Math.max(0, this.dijkstra.path.length - 1)}`],
        ["Dijkstra 搜索格子", `${dVisited}/${this.dijkstra.visitedOrder.length}`],
        ["AI 临场搜索格子", aiMetrics.searched_nodes ?? 0],
        ["AI 少搜索", `${formatNumber(reduction * 100, 1)}%`],
        ["两者路径长度", `${this.dijkstra.pathLength} / ${aiMetrics.steps ?? "-"}`],
        ["AI 检查点", checkpointLabel(this.aiRollout)],
        ["推理耗时", `${formatNumber(aiMetrics.policy_ms, 1)}ms`],
        ...trainingMetricRows(this.trainingState),
        ["传统状态", statusLabel(this.traditionalRunning, this.traditionalDone, !this.dijkstra.success)],
        ["AI 状态", statusLabel(this.aiRunning, this.aiDone, this.aiRollout?.metrics?.success === false)],
      ];
    }

    evaluation() {
      const aiMetrics = this.aiRollout?.metrics || {};
      const nodeRatio = this.dijkstra.visitedOrder.length / Math.max(1, this.w * this.h);
      const gap = aiMetrics.generalization_gap ?? 0;
      return [
        ["鲁棒性", `${SCENARIO[this.scenario].summary}；机器人半径 ${formatNumber(this.robotRadius, 2)} 格，Dijkstra 搜索覆盖约 ${percent(nodeRatio)}。`],
        ["泛化性", `传统算法不用训练，换地图仍可搜索；AI 侧需要看未见地图 gap=${formatNumber(gap, 2)} 是否可接受。`],
        ["可解释性", "Dijkstra 的解释很直接：每个蓝色格子都是按累计代价扩散出来的；AI 需要额外展示动作原因。"],
      ];
    }

    drawbacks() {
      return [
        "Dijkstra 不知道终点方向，只能从起点一圈圈扩散，地图越大搜索越浪费。",
        "每换一张地图都要重新搜索；AI 训练后可以直接根据状态输出下一步动作。",
        "它只能处理明确写出来的代价规则，不能从历史样本里学到常见路线偏好。",
      ];
    }

    legend() {
      return [
        ["障碍物", COLOR.obstacle],
        ["移动障碍", COLOR.dynamic],
        ["Dijkstra 搜索留痕", COLOR.visited],
        ["机器人面积/安全边界", COLOR.path],
        ["AI 训练轨迹", COLOR.actual],
        ["最终路径", COLOR.path],
        ["起点/终点", COLOR.start],
      ];
    }

    secondary() {
      return null;
    }

    resetTraining() {
      this.trainingState = resetRuntimeTraining(this.demoId, this.difficulty, this.scenario);
      this.aiProgress = 0;
      this.aiDone = false;
      this.aiRunning = false;
    }
  }

  function makeDynamicObstacles(difficulty, w, h, scenario = "baseline") {
    const midY = Math.floor(h / 2);
    const scenarioInfo = scenarioConfig(scenario);
    const speedScale = (DIFFICULTY[difficulty]?.multiplier ?? 1.45) * scenarioInfo.speedMultiplier;
    const obstacles = [
      {
        kind: "patrol",
        name: "高速横向障碍",
        x: Math.floor(w * 0.18),
        y: midY - 2,
        w: 5,
        h: 4,
        axis: "x",
        min: Math.floor(w * 0.12),
        max: Math.floor(w * 0.58),
        dir: 1,
        speed: 5.2 * speedScale,
      },
      {
        kind: "patrol",
        name: "纵向巡逻障碍",
        x: Math.floor(w * 0.62),
        y: 6,
        w: 4,
        h: 6,
        axis: "y",
        min: 5,
        max: h - 12,
        dir: 1,
        speed: 3.8 * speedScale,
      },
      {
        kind: "rect",
        name: "矩形轨迹障碍",
        x: Math.floor(w * 0.32),
        y: Math.floor(h * 0.16),
        w: 4,
        h: 4,
        route: [
          { x: Math.floor(w * 0.32), y: Math.floor(h * 0.16) },
          { x: Math.floor(w * 0.50), y: Math.floor(h * 0.16) },
          { x: Math.floor(w * 0.50), y: Math.floor(h * 0.48) },
          { x: Math.floor(w * 0.32), y: Math.floor(h * 0.48) },
        ],
        target: 1,
        speed: 3.2 * speedScale,
      },
      {
        kind: "gate",
        name: "周期门",
        x: Math.floor(w * 0.73) - 2,
        y: midY - 3,
        w: 5,
        h: 7,
        period: (difficulty === "extreme" ? 2.4 : 3.2) / scenarioInfo.speedMultiplier,
        duty: difficulty === "normal" ? 0.42 : scenario === "generalization" ? 0.66 : 0.58,
        phase: 0.7,
        active: true,
      },
    ];

    if (difficulty !== "normal") {
      obstacles.push({
        kind: "patrol",
        name: "下方快车道障碍",
        x: Math.floor(w * 0.15),
        y: Math.floor(h * 0.72) - 2,
        w: 6,
        h: 4,
        axis: "x",
        min: Math.floor(w * 0.08),
        max: Math.floor(w * 0.82),
        dir: -1,
        speed: 4.8 * speedScale,
      });
    }

    if (difficulty === "extreme") {
      obstacles.push({
        kind: "rect",
        name: "外圈巡逻障碍",
        x: Math.floor(w * 0.58),
        y: Math.floor(h * 0.62),
        w: 5,
        h: 5,
        route: [
          { x: Math.floor(w * 0.58), y: Math.floor(h * 0.62) },
          { x: Math.floor(w * 0.86), y: Math.floor(h * 0.62) },
          { x: Math.floor(w * 0.86), y: Math.floor(h * 0.82) },
          { x: Math.floor(w * 0.58), y: Math.floor(h * 0.82) },
        ],
        target: 1,
        speed: 3.9 * speedScale,
      });
      obstacles.push({
        kind: "gate",
        name: "短周期门",
        x: Math.floor(w * 0.36) - 2,
        y: Math.floor(h * 0.70) - 3,
        w: 5,
        h: 7,
        period: 1.8,
        duty: 0.52,
        phase: 0.2,
        active: true,
      });
    }

    if (scenario !== "baseline") {
      obstacles.push({
        kind: "diagonal",
        name: "斜向穿行障碍",
        x: Math.floor(w * 0.28),
        y: Math.floor(h * 0.20),
        w: 4,
        h: 4,
        minX: Math.floor(w * 0.18),
        maxX: Math.floor(w * 0.78),
        minY: Math.floor(h * 0.18),
        maxY: Math.floor(h * 0.76),
        dirX: 1,
        dirY: 1,
        speedX: 3.4 * speedScale,
        speedY: 2.2 * speedScale,
      });
    }

    return obstacles;
  }

  class DynamicAStarDemo {
    constructor(difficulty = "hard", scenario = "baseline") {
      this.demoId = "dynamic";
      this.difficulty = difficulty;
      this.scenario = scenario;
      this.title = "A* 动态障碍";
      this.claim = "A* 规划的是当前地图。多个高速障碍按不同轨迹移动时，旧路径很快失效，只能反复重算。";
      this.mapSerial = 0;
      this.mapSeed = 0;
      this.reset(true);
    }

    reset(forceNewMap = false) {
      ensureMapIdentity(this, forceNewMap);
      this.scenarioInfo = scenarioConfig(this.scenario);
      this.trainingState = getRuntimeTraining(this.demoId, this.difficulty, this.scenario);
      this.robotRadius = gridRobotRadius(this.difficulty, this.scenario) + 0.1;
      let selected = null;
      for (let attempt = 0; attempt < 35; attempt += 1) {
        const map = makeDynamicMap(this.difficulty, this.scenario, this.mapSeed + attempt * 6151);
        const obstacles = makeDynamicObstacles(this.difficulty, map.w, map.h, this.scenario);
        const probe = gridSearch({
          grid: map.grid,
          w: map.w,
          h: map.h,
          start: map.start,
          goal: map.goal,
          heuristic: manhattan,
          extraBlocked: (x, y) => isGridFootprintBlocked(
            map.grid,
            map.w,
            map.h,
            x,
            y,
            this.robotRadius,
            (checkX, checkY) => isMovingObstacleCell(obstacles, checkX, checkY),
          ),
        });
        if (probe.success) {
          selected = { map, obstacles };
          break;
        }
      }
      if (!selected) {
        const map = makeDynamicMap(this.difficulty, "baseline", this.mapSeed);
        selected = { map, obstacles: makeDynamicObstacles(this.difficulty, map.w, map.h, this.scenario) };
      }
      Object.assign(this, selected.map);
      this.agent = { ...this.start };
      this.agentTrail = [{ ...this.agent }];
      this.obstacles = selected.obstacles;
      this.searchLayers = [];
      this.oldPaths = [];
      this.currentPath = [];
      this.pathIndex = 0;
      this.planCalls = 0;
      this.replans = 0;
      this.totalVisited = 0;
      this.blockedEvents = 0;
      this.waitEvents = 0;
      this.nearMisses = 0;
      this.failedPlans = 0;
      this.reached = false;
      this.running = false;
      this.traditionalRunning = false;
      this.aiRunning = false;
      this.traditionalDone = false;
      this.aiDone = false;
      this.timeSec = 0;
      this.agentAcc = 0;
      this.aiProgress = 0;
      this.aiRollout = makeAiGridRollout(
        this.grid,
        this.w,
        this.h,
        this.start,
        this.goal,
        getAiRollout("astarDynamic", this.difficulty),
        this.difficulty,
        this.scenario,
        "dynamic",
        (x, y) => this.isRobotBlockedCell(x, y),
      );
      this.agentDelay = (this.difficulty === "extreme" ? 95 : this.difficulty === "hard" ? 115 : 135) + this.scenarioInfo.latencyMs * 0.35;
      this.replan(true);
    }

    run() {
      this.reset(true);
      this.trainingState = trainRuntimeAi(this.demoId, this.difficulty, this.scenario);
      this.traditionalRunning = true;
      this.aiRunning = true;
      this.traditionalDone = false;
      this.aiDone = false;
      this.running = true;
    }

    pause() {
      this.traditionalRunning = false;
      this.aiRunning = false;
      this.running = false;
    }

    step() {
      const previousTraditional = this.traditionalRunning;
      const previousAi = this.aiRunning;
      this.traditionalRunning = !this.traditionalDone;
      this.aiRunning = !this.aiDone;
      this.update(180, 5);
      this.traditionalRunning = previousTraditional && !this.traditionalDone;
      this.aiRunning = previousAi && !this.aiDone;
      this.running = this.traditionalRunning || this.aiRunning;
    }

    activeDynamicRects() {
      return this.obstacles.filter((obstacle) => obstacle.kind !== "gate" || obstacle.active);
    }

    isDynamicCell(x, y) {
      return this.activeDynamicRects().some((obstacle) => (
        x + 1 > obstacle.x &&
        x < obstacle.x + obstacle.w &&
        y + 1 > obstacle.y &&
        y < obstacle.y + obstacle.h
      ));
    }

    isRobotBlockedCell(x, y) {
      return isGridFootprintBlocked(
        this.grid,
        this.w,
        this.h,
        x,
        y,
        this.robotRadius,
        (checkX, checkY) => this.isDynamicCell(checkX, checkY),
      );
    }

    moveDynamicObstacles(dt, speed) {
      const speedFactor = Math.max(0.3, speed / 5);
      const dtSec = (dt / 1000) * speedFactor;
      this.timeSec += dtSec;

      this.obstacles.forEach((obstacle) => {
        if (obstacle.kind === "patrol") {
          const next = obstacle[obstacle.axis] + obstacle.dir * obstacle.speed * dtSec;
          if (next < obstacle.min) {
            obstacle[obstacle.axis] = obstacle.min + (obstacle.min - next);
            obstacle.dir = 1;
          } else if (next > obstacle.max) {
            obstacle[obstacle.axis] = obstacle.max - (next - obstacle.max);
            obstacle.dir = -1;
          } else {
            obstacle[obstacle.axis] = next;
          }
        }

        if (obstacle.kind === "rect") {
          let remaining = obstacle.speed * dtSec;
          while (remaining > 0) {
            const target = obstacle.route[obstacle.target];
            const dx = target.x - obstacle.x;
            const dy = target.y - obstacle.y;
            const dist = Math.hypot(dx, dy);
            if (dist < 0.001) {
              obstacle.target = (obstacle.target + 1) % obstacle.route.length;
              continue;
            }
            if (dist <= remaining) {
              obstacle.x = target.x;
              obstacle.y = target.y;
              obstacle.target = (obstacle.target + 1) % obstacle.route.length;
              remaining -= dist;
            } else {
              obstacle.x += (dx / dist) * remaining;
              obstacle.y += (dy / dist) * remaining;
              remaining = 0;
            }
          }
        }

        if (obstacle.kind === "diagonal") {
          const nextX = obstacle.x + obstacle.dirX * obstacle.speedX * dtSec;
          const nextY = obstacle.y + obstacle.dirY * obstacle.speedY * dtSec;
          if (nextX < obstacle.minX || nextX > obstacle.maxX) obstacle.dirX *= -1;
          if (nextY < obstacle.minY || nextY > obstacle.maxY) obstacle.dirY *= -1;
          obstacle.x = clamp(nextX, obstacle.minX, obstacle.maxX);
          obstacle.y = clamp(nextY, obstacle.minY, obstacle.maxY);
        }

        if (obstacle.kind === "gate") {
          const cycle = (this.timeSec + obstacle.phase) % obstacle.period;
          obstacle.active = cycle < obstacle.period * obstacle.duty;
        }
      });
    }

    maxObstacleSpeed() {
      return movingObstacleMaxSpeed(this.obstacles);
    }

    replan(isInitial = false) {
      if (!isInitial && this.currentPath.length) this.oldPaths.push(this.currentPath);
      const result = gridSearch({
        grid: this.grid,
        w: this.w,
        h: this.h,
        start: this.agent,
        goal: this.goal,
        heuristic: manhattan,
        extraBlocked: (x, y) => this.isRobotBlockedCell(x, y),
      });
      this.planCalls += 1;
      if (!isInitial) this.replans += 1;
      this.totalVisited += result.visitedOrder.length;
      if (!result.success) this.failedPlans += 1;
      this.currentPath = result.path;
      this.pathIndex = 0;
      this.searchLayers.push({
        visitedOrder: result.visitedOrder,
        path: result.path,
        progress: 0,
        timeMs: result.timeMs,
        success: result.success,
      });
      if (this.searchLayers.length > 18) this.searchLayers.shift();
      if (this.oldPaths.length > 12) this.oldPaths.shift();
    }

    remainingPathBlocked(limit = 28) {
      const end = Math.min(this.currentPath.length, this.pathIndex + limit);
      for (let i = this.pathIndex + 1; i < end; i += 1) {
        const p = this.currentPath[i];
        if (this.isRobotBlockedCell(p.x, p.y)) return true;
      }
      return false;
    }

    moveAgent() {
      if (this.reached) return;
      if (this.isRobotBlockedCell(this.agent.x, this.agent.y)) {
        this.nearMisses += 1;
        this.replan(false);
        return;
      }
      if (!this.currentPath.length || this.pathIndex >= this.currentPath.length - 1) {
        if (this.agent.x === this.goal.x && this.agent.y === this.goal.y) {
          this.reached = true;
          this.traditionalDone = true;
          this.traditionalRunning = false;
        } else {
          this.waitEvents += 1;
          this.replan(false);
        }
        return;
      }

      if (this.remainingPathBlocked()) {
        this.blockedEvents += 1;
        this.replan(false);
        return;
      }

      const next = this.currentPath[this.pathIndex + 1];
      if (this.isRobotBlockedCell(next.x, next.y)) {
        this.blockedEvents += 1;
        this.replan(false);
        return;
      }
      this.agent = { ...next };
      this.agentTrail.push({ ...this.agent });
      if (this.agentTrail.length > 220) this.agentTrail.shift();
      this.pathIndex += 1;
      if (this.agent.x === this.goal.x && this.agent.y === this.goal.y) {
        this.reached = true;
        this.traditionalDone = true;
        this.traditionalRunning = false;
      }
    }

    update(dt, speed) {
      const visibleSpeed = Math.max(1, speed);
      this.searchLayers.forEach((layer) => {
        layer.progress = Math.min(layer.visitedOrder.length, layer.progress + dt * (0.2 + visibleSpeed * 0.08));
      });
      if (!this.traditionalRunning && !this.aiRunning) return;
      if (this.aiRunning && !this.aiDone) {
        this.aiProgress += dt * (0.016 + visibleSpeed * 0.011);
        if (this.aiProgress > (this.aiRollout?.path?.length || 0) + 10) {
          this.aiDone = true;
          this.aiRunning = false;
        }
      }

      this.moveDynamicObstacles(dt, visibleSpeed);
      this.agentAcc += dt * (visibleSpeed / 5);

      while (this.traditionalRunning && this.agentAcc >= this.agentDelay) {
        this.moveAgent();
        this.agentAcc -= this.agentDelay;
      }
      this.running = this.traditionalRunning || this.aiRunning;
    }

    render(width, height) {
      const boxes = splitBoxes(width, height, 2);
      this.renderTraditional(boxes[0]);
      this.renderAi(boxes[1]);
    }

    renderTraditional(box) {
      drawBox(box, "A* 动态障碍", "机器人带半径，橙色障碍按多种轨迹移动");
      const geom = getGridGeometry(box, this.w, this.h, 54);
      drawGridBase(this.grid, this.w, this.h, geom);
      this.drawDynamicRoutes(geom);

      this.searchLayers.forEach((layer, indexValue) => {
        const isLast = indexValue === this.searchLayers.length - 1;
        drawVisited(layer.visitedOrder, geom, isLast ? COLOR.visited : COLOR.visitedAlt, isLast ? layer.progress : layer.visitedOrder.length, isLast ? 0.24 : 0.08);
      });
      this.oldPaths.forEach((path) => drawGridPath(path, geom, COLOR.oldPath, 2, 0.42));
      drawGridPath(this.currentPath.slice(this.pathIndex), geom, COLOR.path, 3, 1);
      drawGridPath(this.agentTrail, geom, COLOR.actual, 2.8, 0.82);

      this.drawDynamicObstacles(geom);

      drawGridRobotFootprint({ x: this.agent.x + 0.5, y: this.agent.y + 0.5 }, geom, this.robotRadius, COLOR.actual, "R");
      drawMarker(this.goal, geom, COLOR.goal, "G");
      drawMetricText(box, [
        this.reached ? "状态: 已到达终点" : "状态: 路径跟随中",
        `规划调用 ${this.planCalls} | 重新规划 ${this.replans} | 失效 ${this.blockedEvents} | 等待 ${this.waitEvents}`,
      ]);
    }

    renderAi(box) {
      drawGridAiPanel(
        box,
        "AI 动态策略",
        "训练样本里包含等待动作，遇到门和快障碍时少重算",
        this.grid,
        this.w,
        this.h,
        this.start,
        this.goal,
        this.aiRollout,
        this.aiProgress,
        {
          waitSteps: this.aiRollout?.waitSteps || [],
          robotRadiusCells: this.robotRadius,
          robotLabel: "A",
          afterBase: (geom) => {
            this.drawDynamicRoutes(geom);
            this.drawDynamicObstacles(geom);
          },
        },
      );
    }

    drawDynamicRoutes(geom) {
      ctx.save();
      ctx.strokeStyle = "rgba(247, 152, 36, 0.34)";
      ctx.lineWidth = 2;
      ctx.setLineDash([6, 5]);
      this.obstacles.forEach((obstacle) => {
        if (obstacle.kind === "patrol") {
          const y = geom.y + (obstacle.y + obstacle.h / 2) * geom.cell;
          const xA = geom.x + (obstacle.axis === "x" ? obstacle.min : obstacle.x + obstacle.w / 2) * geom.cell;
          const xB = geom.x + (obstacle.axis === "x" ? obstacle.max + obstacle.w : obstacle.x + obstacle.w / 2) * geom.cell;
          const yA = geom.y + (obstacle.axis === "y" ? obstacle.min : obstacle.y + obstacle.h / 2) * geom.cell;
          const yB = geom.y + (obstacle.axis === "y" ? obstacle.max + obstacle.h : obstacle.y + obstacle.h / 2) * geom.cell;
          ctx.beginPath();
          ctx.moveTo(xA, obstacle.axis === "x" ? y : yA);
          ctx.lineTo(xB, obstacle.axis === "x" ? y : yB);
          ctx.stroke();
        }
        if (obstacle.kind === "rect") {
          ctx.beginPath();
          obstacle.route.forEach((point, indexValue) => {
            const x = geom.x + (point.x + obstacle.w / 2) * geom.cell;
            const y = geom.y + (point.y + obstacle.h / 2) * geom.cell;
            if (indexValue === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
          });
          const first = obstacle.route[0];
          ctx.lineTo(geom.x + (first.x + obstacle.w / 2) * geom.cell, geom.y + (first.y + obstacle.h / 2) * geom.cell);
          ctx.stroke();
        }
        if (obstacle.kind === "diagonal") {
          ctx.beginPath();
          ctx.moveTo(geom.x + (obstacle.minX + obstacle.w / 2) * geom.cell, geom.y + (obstacle.minY + obstacle.h / 2) * geom.cell);
          ctx.lineTo(geom.x + (obstacle.maxX + obstacle.w / 2) * geom.cell, geom.y + (obstacle.maxY + obstacle.h / 2) * geom.cell);
          ctx.stroke();
        }
      });
      ctx.restore();
    }

    drawDynamicObstacles(geom) {
      this.obstacles.forEach((obstacle) => {
        ctx.save();
        ctx.globalAlpha = obstacle.kind === "gate" && !obstacle.active ? 0.22 : 0.94;
        ctx.fillStyle = COLOR.dynamic;
        ctx.strokeStyle = COLOR.dynamic;
        ctx.lineWidth = 2;
        const x = geom.x + obstacle.x * geom.cell;
        const y = geom.y + obstacle.y * geom.cell;
        const w = obstacle.w * geom.cell;
        const h = obstacle.h * geom.cell;
        if (obstacle.kind === "gate" && !obstacle.active) {
          ctx.strokeRect(x, y, w, h);
        } else {
          ctx.fillRect(x, y, w, h);
        }
        ctx.restore();
      });
    }

    metrics() {
      const latest = this.searchLayers[this.searchLayers.length - 1];
      const avgVisited = this.planCalls ? this.totalVisited / this.planCalls : 0;
      return [
        ["实验强度", DIFFICULTY[this.difficulty]?.label ?? this.difficulty],
        ["环境模式", SCENARIO[this.scenario]?.label ?? this.scenario],
        ["地图编号", `#${this.mapSerial}`],
        ["移动障碍数", this.obstacles.length],
        ["最快障碍速度", `${formatNumber(this.maxObstacleSpeed(), 1)} 格/秒`],
        ["模拟延迟", `${formatNumber(this.scenarioInfo.latencyMs, 0)}ms`],
        ["机器人半径", `${formatNumber(this.robotRadius, 2)} 格`],
        ["机器人位置", `${this.agent.x},${this.agent.y}`],
        ["规划调用", this.planCalls],
        ["重新规划次数", this.replans],
        ["路径失效次数", this.blockedEvents],
        ["等待/无路次数", this.waitEvents],
        ["贴脸风险次数", this.nearMisses],
        ["累计搜索格子", this.totalVisited],
        ["平均每次搜索", `${formatNumber(avgVisited, 0)} 格`],
        ["最近一次搜索", latest ? `${latest.visitedOrder.length} 格 / ${formatNumber(latest.timeMs, 2)}ms` : "-"],
        ["AI 重规划次数", this.aiRollout?.metrics?.replans ?? 0],
        ["AI 等待动作", this.aiRollout?.metrics?.waits ?? "-"],
        ["AI 检查点", checkpointLabel(this.aiRollout)],
        ...trainingMetricRows(this.trainingState),
        ["传统状态", statusLabel(this.traditionalRunning, this.traditionalDone, false)],
        ["AI 状态", statusLabel(this.aiRunning, this.aiDone, this.aiRollout?.metrics?.success === false)],
      ];
    }

    evaluation() {
      const avgVisited = this.planCalls ? this.totalVisited / this.planCalls : 0;
      const risk = clamp((this.blockedEvents + this.nearMisses + this.waitEvents) / Math.max(1, this.planCalls + 4), 0, 1);
      return [
        ["鲁棒性", `机器人半径 ${formatNumber(this.robotRadius, 2)} 格，速度倍率 ${formatNumber(this.scenarioInfo.speedMultiplier, 2)}，延迟 ${formatNumber(this.scenarioInfo.latencyMs, 0)}ms；传统 A* 风险率约 ${percent(risk)}。`],
        ["泛化性", `泛化场景会改变障碍轨迹和门周期，A* 可重新搜索，但会付出平均 ${formatNumber(avgVisited, 0)} 格/次的搜索成本。`],
        ["可解释性", "A* 的旧路径、当前路径和搜索层都可追踪；AI 侧需要解释为什么等待、减速或绕行。"],
      ];
    }

    drawbacks() {
      return [
        "A* 默认只看当前静态地图，障碍一动，刚算好的路径就可能作废。",
        "它遇到动态变化通常靠反复重规划，速度越快、障碍越多，计算压力越明显。",
        "AI 可以把等待、减速、绕行这些动作学进策略里，不一定每次都从头搜索。",
      ];
    }

    legend() {
      return [
        ["静态障碍", COLOR.obstacle],
        ["动态障碍", COLOR.dynamic],
        ["障碍运动轨迹", COLOR.dynamic],
        ["机器人面积/安全边界", COLOR.actual],
        ["A* 搜索留痕", COLOR.visited],
        ["旧路径", COLOR.oldPath],
        ["当前路径", COLOR.path],
        ["AI 训练轨迹", COLOR.actual],
        ["机器人/目标", COLOR.start],
      ];
    }

    secondary() {
      return null;
    }

    resetTraining() {
      this.trainingState = resetRuntimeTraining(this.demoId, this.difficulty, this.scenario);
      this.aiProgress = 0;
      this.aiDone = false;
      this.aiRunning = false;
    }
  }

  function fitAreaToBox(box, areaW, areaH, titleHeight = 50) {
    const pad = 16;
    const availableW = box.w - pad * 2;
    const availableH = box.h - pad * 2 - titleHeight;
    const scale = Math.min(availableW / areaW, availableH / areaH);
    const drawW = areaW * scale;
    const drawH = areaH * scale;
    return {
      x: box.x + pad + (availableW - drawW) / 2,
      y: box.y + titleHeight + pad + (availableH - drawH) / 2,
      w: drawW,
      h: drawH,
      scale,
    };
  }

  function pointInRect(point, rect) {
    return point.x >= rect.x && point.x <= rect.x + rect.w && point.y >= rect.y && point.y <= rect.y + rect.h;
  }

  function pointInInflatedRect(point, rect, radius = 0) {
    return (
      point.x >= rect.x - radius &&
      point.x <= rect.x + rect.w + radius &&
      point.y >= rect.y - radius &&
      point.y <= rect.y + rect.h + radius
    );
  }

  function segmentBlocked(a, b, rects, areaW, areaH, radius = 0) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const length = Math.hypot(dx, dy);
    const steps = Math.max(2, Math.ceil(length / 5));
    for (let i = 0; i <= steps; i += 1) {
      const t = i / steps;
      const p = { x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t) };
      if (p.x < radius || p.y < radius || p.x > areaW - radius || p.y > areaH - radius) return true;
      for (const rect of rects) {
        if (pointInInflatedRect(p, rect, radius)) return true;
      }
    }
    return false;
  }

  function pathLengthContinuous(path) {
    let length = 0;
    for (let i = 1; i < path.length; i += 1) {
      length += Math.hypot(path[i].x - path[i - 1].x, path[i].y - path[i - 1].y);
    }
    return length;
  }

  function drawContinuousPath(path, sx, sy, color, width, limit = path?.length || 0, alpha = 1) {
    if (!path || path.length < 2) return;
    const capped = Math.min(path.length, Math.max(2, Math.floor(limit)));
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.strokeStyle = color;
    ctx.lineWidth = width;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.beginPath();
    for (let i = 0; i < capped; i += 1) {
      const point = path[i];
      if (i === 0) ctx.moveTo(sx(point.x), sy(point.y));
      else ctx.lineTo(sx(point.x), sy(point.y));
    }
    ctx.stroke();
    ctx.restore();
  }

  function drawContinuousSamples(samples, sx, sy, color, limit, radius = 2.5, alpha = 0.55) {
    if (!samples || samples.length === 0) return;
    const capped = Math.min(samples.length, Math.floor(limit));
    ctx.save();
    ctx.globalAlpha = alpha;
    ctx.fillStyle = color;
    for (let i = 0; i < capped; i += 1) {
      const point = samples[i];
      ctx.beginPath();
      ctx.arc(sx(point.x), sy(point.y), radius, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.restore();
  }

  function drawSafetyCircle(x, y, radius, color) {
    ctx.save();
    ctx.globalAlpha = 0.16;
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
    ctx.globalAlpha = 0.8;
    ctx.strokeStyle = color;
    ctx.lineWidth = 1;
    ctx.stroke();
    ctx.restore();
  }

  class RRTDemo {
    constructor(difficulty = "hard", scenario = "baseline") {
      this.demoId = "rrt";
      this.difficulty = difficulty;
      this.scenario = scenario;
      this.title = "RRT 窄通道";
      this.claim = "RRT 靠随机采样长树。窄门太小的时候，采样不一定打中门口，所以同一个任务每次结果都可能不同。";
      this.areaW = 1000;
      this.areaH = 560;
      this.mapSerial = 0;
      this.mapSeed = 0;
      this.reset(true);
    }

    reset(forceNewMap = false) {
      ensureMapIdentity(this, forceNewMap);
      const config = {
        normal: { gap: 44, maxIterations: 1600, stepSize: 20, goalRadius: 28, goalBias: 0.07 },
        hard: { gap: 30, maxIterations: 2200, stepSize: 17, goalRadius: 24, goalBias: 0.05 },
        extreme: { gap: 22, maxIterations: 3000, stepSize: 14, goalRadius: 20, goalBias: 0.035 },
      }[this.difficulty] ?? { gap: 30, maxIterations: 2200, stepSize: 17, goalRadius: 24, goalBias: 0.05 };
      this.scenarioInfo = scenarioConfig(this.scenario);
      this.trainingState = getRuntimeTraining(this.demoId, this.difficulty, this.scenario);
      const rng = mulberry32(scenarioSeed("rrt-map", this.difficulty, this.scenario, this.mapSeed));
      const gapPenalty = this.scenario === "baseline" ? 0 : this.scenario === "perturbed" ? 4 : 8;
      this.robotRadius = 7 + (this.difficulty === "extreme" ? 3 : this.difficulty === "hard" ? 2 : 0) + this.scenarioInfo.generalization * 3;
      const gap = Math.max(this.robotRadius * 2 + 12, config.gap - gapPenalty);
      const mid = this.areaH / 2;
      this.gap = gap;
      const jitter = this.scenario === "baseline" ? 10 : this.scenario === "perturbed" ? 20 : 32;
      const j = () => (rng() * 2 - 1) * jitter;
      this.rects = [
        { x: 482, y: 0, w: 46, h: mid - gap / 2 },
        { x: 482, y: mid + gap / 2, w: 46, h: mid - gap / 2 },
        { x: 675 + j(), y: 72 + j() * 0.3, w: 86, h: 170 },
        { x: 690 + j(), y: 320 + j() * 0.3, w: 90, h: 150 },
        {
          kind: "patrol",
          mobile: true,
          x: 360 + j(),
          y: 86,
          w: 62,
          h: 46,
          axis: "y",
          min: 72,
          max: 210,
          dir: rng() > 0.5 ? 1 : -1,
          speed: 28 * (DIFFICULTY[this.difficulty]?.multiplier ?? 1.2) * this.scenarioInfo.speedMultiplier,
        },
      ];
      if (this.difficulty !== "normal") {
        this.rects.push(
          { x: 250 + j(), y: 70 + j() * 0.2, w: 78, h: 156 },
          { x: 245 + j(), y: 338 + j() * 0.2, w: 95, h: 120 },
          {
            kind: "patrol",
            mobile: true,
            x: 572,
            y: 214,
            w: 80,
            h: 40,
            axis: "x",
            min: 552,
            max: 650,
            dir: rng() > 0.5 ? 1 : -1,
            speed: 22 * (DIFFICULTY[this.difficulty]?.multiplier ?? 1.2) * this.scenarioInfo.speedMultiplier,
          },
        );
      }
      if (this.difficulty === "extreme") {
        this.rects.push(
          { x: 360 + j() * 0.4, y: 248, w: 74, h: 28 },
          { x: 808, y: 188, w: 64, h: 160 },
        );
      }
      if (this.scenario !== "baseline") {
        const shift = this.scenarioInfo.obstacleShift * 8;
        this.rects.push(
          { x: 394 + shift * 0.3, y: 120, w: 42, h: 90 },
          { x: 562, y: 346 - shift * 0.2, w: 70, h: 64 },
        );
      }
      if (this.scenario === "generalization") {
        this.rects.push(
          { x: 148, y: 220, w: 74, h: 118 },
          { x: 820, y: 78, w: 54, h: 120 },
        );
      }
      this.start = { x: 75, y: 280 };
      this.goal = { x: 930, y: 280 };
      this.nodes = [{ ...this.start, parent: -1 }];
      this.movingObstacles = this.rects.filter((rect) => rect.mobile);
      this.timeSec = 0;
      this.iterations = 0;
      this.maxIterations = Math.max(600, Math.floor(config.maxIterations * (this.scenario === "baseline" ? 1 : 0.85)));
      this.stepSize = config.stepSize;
      this.goalRadius = config.goalRadius;
      this.goalBias = Math.max(0.02, config.goalBias - this.scenarioInfo.generalization * 0.012);
      this.finalPath = [];
      this.success = false;
      this.failed = false;
      this.running = false;
      this.traditionalRunning = false;
      this.aiRunning = false;
      this.traditionalDone = false;
      this.aiDone = false;
      this.rng = mulberry32(scenarioSeed("rrt-sampling", this.difficulty, this.scenario, this.mapSeed));
      this.lastTrialSummary = "还没跑";
      this.aiProgress = 0;
      this.aiRollout = getAiRollout("rrtNarrow", this.difficulty);
    }

    run() {
      this.reset(true);
      this.trainingState = trainRuntimeAi(this.demoId, this.difficulty, this.scenario);
      this.traditionalRunning = true;
      this.aiRunning = true;
      this.traditionalDone = false;
      this.aiDone = false;
      this.running = true;
    }

    pause() {
      this.traditionalRunning = false;
      this.aiRunning = false;
      this.running = false;
    }

    step() {
      this.timeSec = advanceMovingObstacles(this.movingObstacles, 180, 5, this.timeSec);
      if (!this.traditionalDone) {
        for (let i = 0; i < 8; i += 1) this.extendTree();
      }
      if (!this.aiDone) {
        this.aiProgress += 3;
        if (this.aiProgress > (this.aiRollout?.path?.length || 0) + 6) {
          this.aiDone = true;
          this.aiRunning = false;
        }
      }
      this.running = this.traditionalRunning || this.aiRunning;
    }

    samplePoint() {
      if (this.rng() < this.goalBias) return { ...this.goal };
      return { x: this.rng() * this.areaW, y: this.rng() * this.areaH };
    }

    nearestNode(point) {
      let bestIndex = 0;
      let bestDist = Number.POSITIVE_INFINITY;
      this.nodes.forEach((node, indexValue) => {
        const dist = Math.hypot(point.x - node.x, point.y - node.y);
        if (dist < bestDist) {
          bestDist = dist;
          bestIndex = indexValue;
        }
      });
      return bestIndex;
    }

    extendTree() {
      if (this.success || this.failed) return;
      this.iterations += 1;
      const sample = this.samplePoint();
      const nearestIndex = this.nearestNode(sample);
      const nearest = this.nodes[nearestIndex];
      const angle = Math.atan2(sample.y - nearest.y, sample.x - nearest.x);
      const next = {
        x: nearest.x + Math.cos(angle) * this.stepSize,
        y: nearest.y + Math.sin(angle) * this.stepSize,
        parent: nearestIndex,
      };
      if (!segmentBlocked(nearest, next, this.rects, this.areaW, this.areaH, this.robotRadius)) {
        this.nodes.push(next);
        if (Math.hypot(next.x - this.goal.x, next.y - this.goal.y) <= this.goalRadius && !segmentBlocked(next, this.goal, this.rects, this.areaW, this.areaH, this.robotRadius)) {
          this.nodes.push({ ...this.goal, parent: this.nodes.length - 1 });
          this.success = true;
          this.traditionalDone = true;
          this.traditionalRunning = false;
          this.finalPath = this.tracePath(this.nodes.length - 1);
        }
      }
      if (this.iterations >= this.maxIterations && !this.success) {
        this.failed = true;
        this.traditionalDone = true;
        this.traditionalRunning = false;
      }
    }

    tracePath(nodeIndex) {
      const path = [];
      let current = nodeIndex;
      while (current >= 0) {
        const node = this.nodes[current];
        path.push({ x: node.x, y: node.y });
        current = node.parent;
      }
      return path.reverse();
    }

    update(dt, speed) {
      if (!this.traditionalRunning && !this.aiRunning) return;
      this.timeSec = advanceMovingObstacles(this.movingObstacles, dt, speed, this.timeSec);
      if (this.traditionalRunning && !this.traditionalDone) {
        const iterationsThisFrame = Math.max(1, Math.floor(speed * 2 + dt * 0.025 * speed));
        for (let i = 0; i < iterationsThisFrame; i += 1) this.extendTree();
      }
      if (this.aiRunning && !this.aiDone) {
        this.aiProgress += dt * (0.02 + speed * 0.018);
        if (this.aiProgress > (this.aiRollout?.path?.length || 0) + 6) {
          this.aiDone = true;
          this.aiRunning = false;
        }
      }
      this.running = this.traditionalRunning || this.aiRunning;
    }

    simulateTrial(seed) {
      const rng = mulberry32(seed);
      const nodes = [{ ...this.start, parent: -1 }];
      for (let iteration = 1; iteration <= this.maxIterations; iteration += 1) {
        const sample = rng() < this.goalBias ? { ...this.goal } : { x: rng() * this.areaW, y: rng() * this.areaH };
        let bestIndex = 0;
        let bestDist = Number.POSITIVE_INFINITY;
        nodes.forEach((node, indexValue) => {
          const dist = Math.hypot(sample.x - node.x, sample.y - node.y);
          if (dist < bestDist) {
            bestDist = dist;
            bestIndex = indexValue;
          }
        });
        const nearest = nodes[bestIndex];
        const angle = Math.atan2(sample.y - nearest.y, sample.x - nearest.x);
        const next = {
          x: nearest.x + Math.cos(angle) * this.stepSize,
          y: nearest.y + Math.sin(angle) * this.stepSize,
          parent: bestIndex,
        };
        if (segmentBlocked(nearest, next, this.rects, this.areaW, this.areaH, this.robotRadius)) continue;
        nodes.push(next);
        if (Math.hypot(next.x - this.goal.x, next.y - this.goal.y) <= this.goalRadius && !segmentBlocked(next, this.goal, this.rects, this.areaW, this.areaH, this.robotRadius)) {
          nodes.push({ ...this.goal, parent: nodes.length - 1 });
          let path = [];
          let current = nodes.length - 1;
          while (current >= 0) {
            const node = nodes[current];
            path.push({ x: node.x, y: node.y });
            current = node.parent;
          }
          path = path.reverse();
          return { success: true, iterations: iteration, pathLength: pathLengthContinuous(path) };
        }
      }
      return { success: false, iterations: this.maxIterations, pathLength: 0 };
    }

    runTrials() {
      const base = (Date.now() & 0xfffffff) + Math.floor(Math.random() * 10000);
      const trials = Array.from({ length: 20 }, (_, i) => this.simulateTrial(base + i * 9973));
      const successCount = trials.filter((trial) => trial.success).length;
      const successful = trials.filter((trial) => trial.success);
      const avgIterations = successful.length
        ? successful.reduce((sum, trial) => sum + trial.iterations, 0) / successful.length
        : 0;
      const minIterations = successful.length ? Math.min(...successful.map((trial) => trial.iterations)) : 0;
      const maxIterations = successful.length ? Math.max(...successful.map((trial) => trial.iterations)) : 0;
      this.lastTrialSummary = `${successCount}/20 成功，成功样本迭代 ${minIterations}-${maxIterations}，均值 ${formatNumber(avgIterations, 0)}`;
    }

    render(width, height) {
      const boxes = splitBoxes(width, height, 2);
      this.renderTraditional(boxes[0]);
      this.renderAi(boxes[1]);
    }

    renderTraditional(box) {
      drawBox(box, "RRT 窄通道", "树枝是随机采样留下的搜索痕迹，窄门越小越看运气");
      const fit = fitAreaToBox(box, this.areaW, this.areaH, 54);
      const sx = (x) => fit.x + x * fit.scale;
      const sy = (y) => fit.y + y * fit.scale;

      ctx.fillStyle = "#0b0f15";
      ctx.fillRect(fit.x, fit.y, fit.w, fit.h);
      ctx.strokeStyle = "#566276";
      ctx.strokeRect(fit.x, fit.y, fit.w, fit.h);

      this.drawMovingRectRoutes(sx, sy);
      this.rects.forEach((rect) => {
        ctx.fillStyle = rect.mobile ? COLOR.dynamic : COLOR.obstacle;
        ctx.fillRect(sx(rect.x), sy(rect.y), rect.w * fit.scale, rect.h * fit.scale);
      });

      ctx.save();
      ctx.strokeStyle = "rgba(79, 163, 255, 0.25)";
      ctx.lineWidth = 1;
      for (let i = 1; i < this.nodes.length; i += 1) {
        const node = this.nodes[i];
        const parent = this.nodes[node.parent];
        ctx.beginPath();
        ctx.moveTo(sx(parent.x), sy(parent.y));
        ctx.lineTo(sx(node.x), sy(node.y));
        ctx.stroke();
      }
      ctx.restore();

      if (this.finalPath.length) {
        ctx.strokeStyle = COLOR.path;
        ctx.lineWidth = 4;
        ctx.lineJoin = "round";
        ctx.lineCap = "round";
        ctx.beginPath();
        this.finalPath.forEach((point, indexValue) => {
          if (indexValue === 0) ctx.moveTo(sx(point.x), sy(point.y));
          else ctx.lineTo(sx(point.x), sy(point.y));
        });
        ctx.stroke();
      }

      const currentNode = this.nodes[this.nodes.length - 1] || this.start;
      drawSafetyCircle(sx(currentNode.x), sy(currentNode.y), this.robotRadius * fit.scale, COLOR.visited);
      drawCircle(sx(this.start.x), sy(this.start.y), 9, COLOR.start, "S");
      drawCircle(sx(this.goal.x), sy(this.goal.y), 11, COLOR.goal, "G");
      drawMetricText(box, [
        this.success ? "状态: 成功穿过窄门" : this.failed ? "状态: 本轮失败" : "状态: 随机采样中",
        `迭代 ${this.iterations}/${this.maxIterations} | 半径 ${formatNumber(this.robotRadius, 1)}px | ${this.lastTrialSummary}`,
      ]);
    }

    renderAi(box) {
      const rollout = this.aiRollout;
      drawBox(box, "AI 引导采样", "训练后把采样集中到窄门和可通过区域");
      drawTrainingCurve(box, rollout);
      const fit = fitAreaToBox(box, this.areaW, this.areaH, 54);
      const sx = (x) => fit.x + x * fit.scale;
      const sy = (y) => fit.y + y * fit.scale;

      ctx.fillStyle = "#0b0f15";
      ctx.fillRect(fit.x, fit.y, fit.w, fit.h);
      ctx.strokeStyle = "#566276";
      ctx.strokeRect(fit.x, fit.y, fit.w, fit.h);

      this.drawMovingRectRoutes(sx, sy);
      this.rects.forEach((rect) => {
        ctx.fillStyle = rect.mobile ? COLOR.dynamic : COLOR.obstacle;
        ctx.fillRect(sx(rect.x), sy(rect.y), rect.w * fit.scale, rect.h * fit.scale);
      });

      const sampleProgress = this.aiProgress * 3;
      drawContinuousSamples(rollout?.samples, sx, sy, COLOR.actual, sampleProgress, 2.2, 0.55);
      drawContinuousPath(rollout?.path, sx, sy, COLOR.actual, 4, this.aiProgress, 1);
      const aiPath = rollout?.path || [];
      const aiPoint = aiPath[clamp(Math.floor(this.aiProgress) - 1, 0, Math.max(0, aiPath.length - 1))];
      if (aiPoint) drawSafetyCircle(sx(aiPoint.x), sy(aiPoint.y), this.robotRadius * fit.scale, COLOR.actual);
      drawCircle(sx(this.start.x), sy(this.start.y), 9, COLOR.start, "S");
      drawCircle(sx(this.goal.x), sy(this.goal.y), 11, COLOR.goal, "G");
      const gate = rollout?.gate;
      if (gate) drawCircle(sx(gate.x), sy(gate.y), 7, COLOR.path, "");

      const metrics = rollout?.metrics || {};
      drawMetricText(box, [
        `AI: 引导穿门 | ${checkpointLabel(rollout)}`,
        `引导样本 ${Math.min(Math.floor(sampleProgress), rollout?.samples?.length || 0)}/${rollout?.samples?.length || 0} | 推理 ${formatNumber(metrics.policy_ms, 1)}ms`,
        `路径长度 ${formatNumber(pathLengthContinuous(rollout?.path || []), 1)}`,
      ]);
    }

    drawMovingRectRoutes(sx, sy) {
      if (!this.movingObstacles.length) return;
      ctx.save();
      ctx.strokeStyle = "rgba(247, 152, 36, 0.38)";
      ctx.lineWidth = 2;
      ctx.setLineDash([7, 6]);
      this.movingObstacles.forEach((obstacle) => {
        const cx = obstacle.x + obstacle.w / 2;
        const cy = obstacle.y + obstacle.h / 2;
        const xA = obstacle.axis === "x" ? obstacle.min + obstacle.w / 2 : cx;
        const xB = obstacle.axis === "x" ? obstacle.max + obstacle.w / 2 : cx;
        const yA = obstacle.axis === "y" ? obstacle.min + obstacle.h / 2 : cy;
        const yB = obstacle.axis === "y" ? obstacle.max + obstacle.h / 2 : cy;
        ctx.beginPath();
        ctx.moveTo(sx(xA), sy(yA));
        ctx.lineTo(sx(xB), sy(yB));
        ctx.stroke();
      });
      ctx.restore();
    }

    metrics() {
      const pathLength = this.finalPath.length ? pathLengthContinuous(this.finalPath) : 0;
      return [
        ["实验强度", DIFFICULTY[this.difficulty]?.label ?? this.difficulty],
        ["环境模式", SCENARIO[this.scenario]?.label ?? this.scenario],
        ["地图编号", `#${this.mapSerial}`],
        ["移动障碍数", this.movingObstacles.length],
        ["最快障碍速度", `${formatNumber(movingObstacleMaxSpeed(this.movingObstacles), 1)} px/秒`],
        ["窄门宽度", `${this.gap} px`],
        ["机器人安全半径", `${formatNumber(this.robotRadius, 1)} px`],
        ["当前状态", this.success ? "成功" : this.failed ? "失败" : this.running ? "运行中" : "暂停"],
        ["迭代次数", `${this.iterations}/${this.maxIterations}`],
        ["树节点数", this.nodes.length],
        ["最终路径长度", pathLength ? formatNumber(pathLength, 1) : "-"],
        ["20 次快速试验", this.lastTrialSummary],
        ["AI 引导样本", this.aiRollout?.metrics?.guided_samples ?? "-"],
        ["AI 检查点", checkpointLabel(this.aiRollout)],
        ...trainingMetricRows(this.trainingState),
        ["传统状态", statusLabel(this.traditionalRunning, this.traditionalDone, this.failed)],
        ["AI 状态", statusLabel(this.aiRunning, this.aiDone, false)],
      ];
    }

    evaluation() {
      const clearance = Math.max(0, this.gap / 2 - this.robotRadius);
      const successText = this.lastTrialSummary.includes("/20") ? this.lastTrialSummary : "点击快速跑 20 次后可看成功率";
      return [
        ["鲁棒性", `移动障碍 ${this.movingObstacles.length} 个，非点机器人安全余量约 ${formatNumber(clearance, 1)}px；余量越小，RRT 越容易试很多次仍过不去。`],
        ["泛化性", `${SCENARIO[this.scenario].summary}；当前 20 次统计：${successText}。`],
        ["可解释性", "蓝色树枝说明 RRT 试过哪些空间；AI 侧用青色采样点显示它把注意力放在窄门附近。"],
      ];
    }

    drawbacks() {
      return [
        "RRT 靠随机采样长树，窄门越小，越可能长时间采不到关键入口。",
        "同一个场景多跑几次，成功时间和路径质量会明显波动。",
        "AI 可以从样本里学到窄门位置，把采样和动作集中到更可能通过的区域。",
      ];
    }

    legend() {
      return [
        ["障碍物", COLOR.obstacle],
        ["移动障碍轨迹", COLOR.dynamic],
        ["RRT 搜索树", COLOR.visited],
        ["车体/安全边界", COLOR.visited],
        ["找到的路径", COLOR.path],
        ["AI 引导采样", COLOR.actual],
        ["起点/终点", COLOR.start],
      ];
    }

    secondary() {
      return {
        label: "快速跑 20 次",
        action: () => this.runTrials(),
      };
    }

    resetTraining() {
      this.trainingState = resetRuntimeTraining(this.demoId, this.difficulty, this.scenario);
      this.aiProgress = 0;
      this.aiDone = false;
      this.aiRunning = false;
    }
  }

  function drawCircle(x, y, radius, color, label) {
    ctx.fillStyle = color;
    ctx.beginPath();
    ctx.arc(x, y, radius, 0, Math.PI * 2);
    ctx.fill();
    if (label) {
      ctx.fillStyle = "#091016";
      ctx.font = "700 11px Inter, sans-serif";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, x, y + 0.5);
      ctx.textAlign = "start";
      ctx.textBaseline = "alphabetic";
    }
  }

  function cellCenter(point) {
    return { x: point.x + 0.5, y: point.y + 0.5 };
  }

  function distanceToCenters(point, centers) {
    let best = Number.POSITIVE_INFINITY;
    centers.forEach((center) => {
      best = Math.min(best, Math.hypot(point.x - center.x, point.y - center.y));
    });
    return best;
  }

  class CarTrackingDemo {
    constructor(difficulty = "hard", scenario = "baseline") {
      this.demoId = "car";
      this.difficulty = difficulty;
      this.scenario = scenario;
      this.title = "网格路径 vs 小车";
      this.claim = "A* 给的是格子路线，不知道小车有转弯半径。直角路线看起来最短，真实小车可能跟不上。";
      this.mapSerial = 0;
      this.mapSeed = 0;
      this.reset(true);
    }

    reset(forceNewMap = false) {
      ensureMapIdentity(this, forceNewMap);
      this.scenarioInfo = scenarioConfig(this.scenario);
      this.trainingState = getRuntimeTraining(this.demoId, this.difficulty, this.scenario);
      let selected = null;
      for (let attempt = 0; attempt < 35; attempt += 1) {
        const map = makeCarMap(this.difficulty, this.scenario, this.mapSeed + attempt * 4271);
        const searchResult = gridSearch({
          grid: map.grid,
          w: map.w,
          h: map.h,
          start: map.start,
          goal: map.goal,
          heuristic: manhattan,
        });
        if (searchResult.success) {
          selected = { map, searchResult };
          break;
        }
      }
      if (!selected) {
        const map = makeCarMap(this.difficulty, "baseline", this.mapSeed);
        selected = {
          map,
          searchResult: gridSearch({ grid: map.grid, w: map.w, h: map.h, start: map.start, goal: map.goal, heuristic: manhattan }),
        };
      }
      Object.assign(this, selected.map);
      this.searchResult = selected.searchResult;
      this.path = this.searchResult.path;
      this.centers = this.path.map(cellCenter);
      this.movingObstacles = makePathMovingObstacles("car", this.path, this.difficulty, this.scenario, this.mapSeed, { count: this.difficulty === "normal" ? 2 : 3 });
      const startAngle = this.scenario === "baseline" ? 0 : this.scenario === "perturbed" ? 0.18 : -0.28;
      this.pose = { x: this.start.x + 0.5, y: this.start.y + 0.5, theta: startAngle };
      this.waypoint = 1;
      this.trail = [{ x: this.pose.x, y: this.pose.y }];
      this.running = false;
      this.traditionalRunning = false;
      this.aiRunning = false;
      this.traditionalDone = false;
      this.aiDone = false;
      this.reached = false;
      this.collided = false;
      this.collisionPoint = null;
      this.maxError = 0;
      this.sharpTurns = countTurns(this.path);
      const bodyScale = this.scenarioInfo.carScale;
      this.carLength = (this.difficulty === "extreme" ? 2.45 : this.difficulty === "hard" ? 2.25 : 2.05) * bodyScale;
      this.carWidth = (this.difficulty === "extreme" ? 1.22 : this.difficulty === "hard" ? 1.1 : 1.0) * bodyScale;
      this.safetyMargin = this.scenario === "baseline" ? 0.18 : this.scenario === "perturbed" ? 0.25 : 0.32;
      this.carSpeed = (this.difficulty === "normal" ? 0.19 : this.difficulty === "hard" ? 0.22 : 0.25) * (1 + this.scenarioInfo.generalization * 0.08);
      this.maxTurn = (this.difficulty === "normal" ? 0.022 : this.difficulty === "hard" ? 0.016 : 0.011) / (1 + this.scenarioInfo.generalization * 0.35);
      this.minTurnRadius = this.carSpeed / Math.max(0.001, this.maxTurn);
      this.timeSec = 0;
      this.aiProgress = 0;
      this.aiRollout = makeAiCarRollout(
        this.grid,
        this.w,
        this.h,
        this.start,
        this.goal,
        getAiRollout("carControl", this.difficulty),
        this.difficulty,
        this.scenario,
      );
    }

    run() {
      this.reset(true);
      this.trainingState = trainRuntimeAi(this.demoId, this.difficulty, this.scenario);
      this.traditionalRunning = true;
      this.aiRunning = true;
      this.traditionalDone = false;
      this.aiDone = false;
      this.running = true;
    }

    pause() {
      this.traditionalRunning = false;
      this.aiRunning = false;
      this.running = false;
    }

    step() {
      this.timeSec = advanceMovingObstacles(this.movingObstacles, 180, 5, this.timeSec);
      if (!this.traditionalDone) {
        const wasRunning = this.traditionalRunning;
        this.traditionalRunning = true;
        for (let i = 0; i < 8; i += 1) this.advance();
        this.traditionalRunning = wasRunning && !this.traditionalDone;
      }
      if (!this.aiDone) {
        this.aiProgress += 12;
        if (this.aiProgress > (this.aiRollout?.trail?.length || 0) + 8) {
          this.aiDone = true;
          this.aiRunning = false;
        }
      }
      this.running = this.traditionalRunning || this.aiRunning;
    }

    footprintSamples(x, y, theta, includeSafety = true) {
      const length = this.carLength + (includeSafety ? this.safetyMargin * 2 : 0);
      const width = this.carWidth + (includeSafety ? this.safetyMargin * 2 : 0);
      const samples = [];
      const cos = Math.cos(theta);
      const sin = Math.sin(theta);
      for (let ix = -2; ix <= 2; ix += 1) {
        for (let iy = -2; iy <= 2; iy += 1) {
          const lx = (ix / 2) * (length / 2);
          const ly = (iy / 2) * (width / 2);
          samples.push({
            x: x + lx * cos - ly * sin,
            y: y + lx * sin + ly * cos,
          });
        }
      }
      return samples;
    }

    collidesAt(x, y, theta = this.pose.theta) {
      const samples = this.footprintSamples(x, y, theta, true);
      return samples.some((sample) => {
        const cx = Math.floor(sample.x);
        const cy = Math.floor(sample.y);
        return isGridBlocked(this.grid, this.w, this.h, cx, cy) || isMovingObstacleCell(this.movingObstacles, cx, cy);
      });
    }

    sweptCollides(fromPose, toPose) {
      const steps = 5;
      for (let i = 1; i <= steps; i += 1) {
        const t = i / steps;
        const x = lerp(fromPose.x, toPose.x, t);
        const y = lerp(fromPose.y, toPose.y, t);
        const theta = lerp(fromPose.theta, toPose.theta, t);
        if (this.collidesAt(x, y, theta)) return { x, y, theta };
      }
      return null;
    }

    advance() {
      if (!this.traditionalRunning || this.reached || this.collided || this.centers.length < 2) return;
      if (this.waypoint >= this.centers.length) {
        this.reached = true;
        this.running = false;
        return;
      }
      const target = this.centers[this.waypoint];
      const dx = target.x - this.pose.x;
      const dy = target.y - this.pose.y;
      const dist = Math.hypot(dx, dy);
      if (dist < 0.42 && this.waypoint < this.centers.length - 1) {
        this.waypoint += 1;
        return;
      }
      const desired = Math.atan2(dy, dx);
      const delta = normalizeAngle(desired - this.pose.theta);
      const nextTheta = this.pose.theta + clamp(delta, -this.maxTurn, this.maxTurn);
      const nextX = this.pose.x + Math.cos(nextTheta) * this.carSpeed;
      const nextY = this.pose.y + Math.sin(nextTheta) * this.carSpeed;
      const nextPose = { x: nextX, y: nextY, theta: nextTheta };
      const collision = this.sweptCollides(this.pose, nextPose);
      this.pose = nextPose;
      this.trail.push({ x: nextX, y: nextY });
      if (this.trail.length > 1600) this.trail.shift();
      this.maxError = Math.max(this.maxError, distanceToCenters(this.pose, this.centers));

      if (collision) {
        this.collided = true;
        this.traditionalDone = true;
        this.traditionalRunning = false;
        this.collisionPoint = { x: collision.x, y: collision.y };
        return;
      }
      const goalCenter = cellCenter(this.goal);
      if (Math.hypot(nextX - goalCenter.x, nextY - goalCenter.y) < 0.72) {
        this.reached = true;
        this.traditionalDone = true;
        this.traditionalRunning = false;
      }
    }

    update(dt, speed) {
      if (!this.traditionalRunning && !this.aiRunning) return;
      this.timeSec = advanceMovingObstacles(this.movingObstacles, dt, speed, this.timeSec);
      if (this.traditionalRunning && !this.traditionalDone) {
        const steps = Math.max(1, Math.floor(speed * 1.4 + dt * 0.018 * speed));
        for (let i = 0; i < steps; i += 1) this.advance();
      }
      if (this.aiRunning && !this.aiDone) {
        this.aiProgress += dt * (0.035 + speed * 0.016);
        if (this.aiProgress > (this.aiRollout?.trail?.length || 0) + 8) {
          this.aiDone = true;
          this.aiRunning = false;
        }
      }
      this.running = this.traditionalRunning || this.aiRunning;
    }

    render(width, height) {
      const boxes = splitBoxes(width, height, 2);
      this.renderTraditional(boxes[0]);
      this.renderAi(boxes[1]);
    }

    renderTraditional(box) {
      drawBox(box, "网格路径 vs 小车", "黄色是网格路径，青色是带转弯限制的小车轨迹");
      const geom = getGridGeometry(box, this.w, this.h, 54);
      drawGridBase(this.grid, this.w, this.h, geom);
      drawGridMovingRoutes(this.movingObstacles, geom);
      drawVisited(this.searchResult.visitedOrder, geom, COLOR.visited, this.searchResult.visitedOrder.length, 0.12);
      drawGridPath(this.path, geom, COLOR.path, 3, 1);
      this.drawTrail(geom);
      drawGridMovingObstacles(this.movingObstacles, geom);
      this.drawCar(geom);
      if (this.collisionPoint) this.drawCollision(geom);
      drawMarker(this.start, geom, COLOR.start, "S");
      drawMarker(this.goal, geom, COLOR.goal, "G");
      drawMetricText(box, [
        this.collided ? "状态: 小车在直角处撞墙" : this.reached ? "状态: 已到达" : "状态: 正在跟踪 A* 路径",
        `A* 转弯 ${this.sharpTurns} 次 | 最小转弯半径 ${formatNumber(this.minTurnRadius, 1)} 格 | 偏离 ${formatNumber(this.maxError, 2)} 格`,
      ]);
    }

    renderAi(box) {
      const rollout = this.aiRollout;
      drawBox(box, "AI 小车控制", "训练输出连续转向和油门，轨迹不再强跟直角格子");
      drawTrainingCurve(box, rollout);
      const geom = getGridGeometry(box, this.w, this.h, 54);
      drawGridBase(this.grid, this.w, this.h, geom);
      drawGridMovingRoutes(this.movingObstacles, geom);
      drawVisited(rollout?.visited || [], geom, COLOR.actual, this.aiProgress * 2, 0.1);
      drawGridPath(rollout?.path || [], geom, COLOR.path, 2.2, 0.38);
      this.drawAiTrail(geom, rollout, this.aiProgress);
      drawGridMovingObstacles(this.movingObstacles, geom);
      this.drawAiCar(geom, rollout, this.aiProgress);
      drawMarker(this.start, geom, COLOR.start, "S");
      drawMarker(this.goal, geom, COLOR.goal, "G");
      const metrics = rollout?.metrics || {};
      drawMetricText(box, [
        `AI: ${metrics.collisions ? "碰撞" : "平滑通过"} | ${checkpointLabel(rollout)}`,
        `控制点 ${Math.min(Math.floor(this.aiProgress), rollout?.trail?.length || 0)}/${rollout?.trail?.length || 0} | 碰撞 ${metrics.collisions ?? 0}`,
        `平滑度 ${formatNumber((metrics.smoothness ?? 0) * 100, 0)}% | 推理 ${formatNumber(metrics.policy_ms, 1)}ms`,
      ]);
    }

    drawTrail(geom) {
      if (this.trail.length < 2) return;
      ctx.strokeStyle = COLOR.actual;
      ctx.lineWidth = 3;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      this.trail.forEach((point, indexValue) => {
        const px = geom.x + point.x * geom.cell;
        const py = geom.y + point.y * geom.cell;
        if (indexValue === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }

    drawAiTrail(geom, rollout, progress) {
      const trail = progressSlice(rollout?.trail || [], progress);
      if (trail.length < 2) return;
      ctx.strokeStyle = COLOR.actual;
      ctx.lineWidth = 3;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.beginPath();
      trail.forEach((point, indexValue) => {
        const px = geom.x + point.x * geom.cell;
        const py = geom.y + point.y * geom.cell;
        if (indexValue === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.stroke();
    }

    drawAiCar(geom, rollout, progress) {
      const trail = rollout?.trail || [];
      if (!trail.length) return;
      const indexValue = clamp(Math.floor(progress) - 1, 0, trail.length - 1);
      const point = trail[indexValue];
      const prev = trail[Math.max(0, indexValue - 2)] || point;
      const theta = Math.atan2(point.y - prev.y, point.x - prev.x);
      this.drawVehicleBody(geom, { x: point.x, y: point.y, theta }, COLOR.actual, false);
    }

    drawVehicleBody(geom, pose, color, collided = false) {
      const cx = geom.x + pose.x * geom.cell;
      const cy = geom.y + pose.y * geom.cell;
      const bodyW = this.carLength * geom.cell;
      const bodyH = this.carWidth * geom.cell;
      const safeW = (this.carLength + this.safetyMargin * 2) * geom.cell;
      const safeH = (this.carWidth + this.safetyMargin * 2) * geom.cell;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(pose.theta);

      ctx.globalAlpha = 0.14;
      ctx.fillStyle = color;
      roundedRectPath(-safeW / 2, -safeH / 2, safeW, safeH, Math.min(6, safeH / 3));
      ctx.fill();

      ctx.globalAlpha = 0.9;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.4;
      roundedRectPath(-safeW / 2, -safeH / 2, safeW, safeH, Math.min(6, safeH / 3));
      ctx.stroke();

      ctx.globalAlpha = 1;
      ctx.fillStyle = collided ? COLOR.failure : color;
      roundedRectPath(-bodyW / 2, -bodyH / 2, bodyW, bodyH, Math.min(4, bodyH / 3));
      ctx.fill();
      ctx.fillStyle = "#091016";
      ctx.globalAlpha = 0.7;
      ctx.fillRect(bodyW * 0.1, -bodyH * 0.28, bodyW * 0.24, bodyH * 0.56);
      ctx.restore();
    }

    drawCar(geom) {
      this.drawVehicleBody(geom, this.pose, COLOR.actual, this.collided);
    }

    drawCollision(geom) {
      const x = geom.x + this.collisionPoint.x * geom.cell;
      const y = geom.y + this.collisionPoint.y * geom.cell;
      ctx.strokeStyle = COLOR.failure;
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.moveTo(x - 10, y - 10);
      ctx.lineTo(x + 10, y + 10);
      ctx.moveTo(x + 10, y - 10);
      ctx.lineTo(x - 10, y + 10);
      ctx.stroke();
    }

    metrics() {
      return [
        ["实验强度", DIFFICULTY[this.difficulty]?.label ?? this.difficulty],
        ["环境模式", SCENARIO[this.scenario]?.label ?? this.scenario],
        ["地图编号", `#${this.mapSerial}`],
        ["移动障碍数", this.movingObstacles.length],
        ["最快障碍速度", `${formatNumber(movingObstacleMaxSpeed(this.movingObstacles), 1)} 格/秒`],
        ["走廊宽度", `${this.corridorWidth} 格`],
        ["车体尺寸", `${formatNumber(this.carLength, 2)} x ${formatNumber(this.carWidth, 2)} 格`],
        ["安全边界", `${formatNumber(this.safetyMargin, 2)} 格`],
        ["最小转弯半径", `${formatNumber(this.minTurnRadius, 1)} 格`],
        ["走廊变化", `偏移 ${this.scenarioStats?.corridorShift ?? 0} / 变窄 ${this.scenarioStats?.corridorWidthChange ?? 0} 格`],
        ["A* 搜索格子", this.searchResult.visitedOrder.length],
        ["A* 路径长度", this.searchResult.pathLength],
        ["直角转弯次数", this.sharpTurns],
        ["最大跟踪偏离", `${formatNumber(this.maxError, 2)} 格`],
        ["AI 控制进度", `${Math.min(Math.floor(this.aiProgress), this.aiRollout?.trail?.length || 0)}/${this.aiRollout?.trail?.length || 0}`],
        ["AI 碰撞次数", this.aiRollout?.metrics?.collisions ?? "-"],
        ["AI 检查点", checkpointLabel(this.aiRollout)],
        ...trainingMetricRows(this.trainingState),
        ["传统状态", statusLabel(this.traditionalRunning, this.traditionalDone, this.collided)],
        ["AI 状态", statusLabel(this.aiRunning, this.aiDone, false)],
      ];
    }

    evaluation() {
      const clearance = Math.max(0, this.corridorWidth - this.carWidth - this.safetyMargin * 2);
      const aiMetrics = this.aiRollout?.metrics || {};
      return [
        ["鲁棒性", `移动障碍 ${this.movingObstacles.length} 个；小车宽 ${formatNumber(this.carWidth, 2)} 格，安全边界 ${formatNumber(this.safetyMargin, 2)} 格，剩余横向余量约 ${formatNumber(clearance, 2)} 格。`],
        ["泛化性", `${SCENARIO[this.scenario].summary}；走廊偏移/变窄后，传统网格路径仍只保证“点”可走，不保证车身能过。`],
        ["可解释性", `黄色是离散路径，青色矩形是实际车体；最小转弯半径 ${formatNumber(this.minTurnRadius, 1)} 格，AI 侧 gap=${formatNumber(aiMetrics.generalization_gap ?? 0, 2)}。`],
      ];
    }

    drawbacks() {
      return [
        "网格路径只保证格子层面可走，不保证真实小车能按转弯半径执行。",
        "直角拐弯在搜索结果里很正常，但车辆控制里可能直接撞墙或偏离车道。",
        "AI 控制可以直接学习连续转向和油门，让轨迹更贴近真实执行能力。",
      ];
    }

    legend() {
      return [
        ["墙体", COLOR.obstacle],
        ["移动障碍", COLOR.dynamic],
        ["A* 搜索留痕", COLOR.visited],
        ["A* 网格路径", COLOR.path],
        ["小车真实轨迹", COLOR.actual],
        ["车体/安全边界", COLOR.actual],
        ["AI 控制轨迹", COLOR.actual],
        ["碰撞点", COLOR.failure],
      ];
    }

    secondary() {
      return null;
    }

    resetTraining() {
      this.trainingState = resetRuntimeTraining(this.demoId, this.difficulty, this.scenario);
      this.aiProgress = 0;
      this.aiDone = false;
      this.aiRunning = false;
    }
  }

  const factories = {
    search: () => new SearchComparisonDemo(state.difficulty, state.scenario),
    dynamic: () => new DynamicAStarDemo(state.difficulty, state.scenario),
    rrt: () => new RRTDemo(state.difficulty, state.scenario),
    car: () => new CarTrackingDemo(state.difficulty, state.scenario),
  };

  function setActiveDemo(id) {
    state.activeId = id;
    state.activeDemo = factories[id]();
    tabButtons.forEach((button) => {
      button.classList.toggle("active", button.dataset.demo === id);
    });
    syncSidePanel();
    draw();
  }

  function syncSidePanel() {
    const demo = state.activeDemo;
    if (!demo) return;
    demoTitle.textContent = demo.title;
    demoClaim.textContent = demo.claim;
    renderMetrics(demo.metrics());
    renderDrawbacks(demo.drawbacks ? demo.drawbacks() : []);
    renderEvaluation(demo.evaluation ? demo.evaluation() : []);
    renderLegend(demo.legend());
    const secondary = demo.secondary();
    if (secondary) {
      secondaryBtn.hidden = false;
      secondaryBtn.textContent = secondary.label;
      secondaryBtn.onclick = () => {
        secondary.action();
        syncSidePanel();
        draw();
      };
    } else {
      secondaryBtn.hidden = true;
      secondaryBtn.onclick = null;
    }
  }

  function renderMetrics(rows) {
    metricsEl.replaceChildren();
    rows.forEach(([label, value]) => {
      const row = document.createElement("div");
      row.className = "metric-row";
      const labelEl = document.createElement("span");
      labelEl.textContent = label;
      const valueEl = document.createElement("span");
      valueEl.textContent = String(value);
      row.append(labelEl, valueEl);
      metricsEl.append(row);
    });
  }

  function renderDrawbacks(rows) {
    drawbacksEl.replaceChildren();
    rows.forEach((text) => {
      const item = document.createElement("li");
      item.textContent = text;
      drawbacksEl.append(item);
    });
  }

  function renderEvaluation(rows) {
    evaluationEl.replaceChildren();
    rows.forEach(([label, text]) => {
      const row = document.createElement("div");
      row.className = "evaluation-row";
      const labelEl = document.createElement("strong");
      labelEl.textContent = label;
      const textEl = document.createElement("span");
      textEl.textContent = text;
      row.append(labelEl, textEl);
      evaluationEl.append(row);
    });
  }

  function renderLegend(rows) {
    legendEl.replaceChildren();
    rows.forEach(([label, color]) => {
      const row = document.createElement("div");
      row.className = "legend-row";
      const swatch = document.createElement("span");
      swatch.className = "swatch";
      swatch.style.background = color;
      row.append(swatch, document.createTextNode(label));
      legendEl.append(row);
    });
  }

  function resizeCanvas() {
    const rect = canvas.getBoundingClientRect();
    state.dpr = Math.min(window.devicePixelRatio || 1, 2);
    state.viewW = Math.max(320, rect.width);
    state.viewH = Math.max(360, rect.height);
    canvas.width = Math.floor(state.viewW * state.dpr);
    canvas.height = Math.floor(state.viewH * state.dpr);
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    draw();
  }

  function draw() {
    ctx.clearRect(0, 0, state.viewW, state.viewH);
    ctx.fillStyle = COLOR.bg;
    ctx.fillRect(0, 0, state.viewW, state.viewH);
    if (state.activeDemo) state.activeDemo.render(state.viewW, state.viewH);
  }

  function loop(timestamp) {
    if (!state.lastTime) state.lastTime = timestamp;
    const dt = Math.min(50, timestamp - state.lastTime);
    state.lastTime = timestamp;
    if (state.activeDemo) {
      state.activeDemo.update(dt, state.speed);
      draw();
      renderMetrics(state.activeDemo.metrics());
      renderEvaluation(state.activeDemo.evaluation ? state.activeDemo.evaluation() : []);
    }
    requestAnimationFrame(loop);
  }

  tabButtons.forEach((button) => {
    button.addEventListener("click", () => setActiveDemo(button.dataset.demo));
  });

  difficultyButtons.forEach((button) => {
    button.addEventListener("click", () => {
      state.difficulty = button.dataset.difficulty;
      difficultyButtons.forEach((item) => {
        item.classList.toggle("active", item.dataset.difficulty === state.difficulty);
      });
      setActiveDemo(state.activeId);
    });
  });

  scenarioButtons.forEach((button) => {
    button.addEventListener("click", () => {
      state.scenario = button.dataset.scenario;
      scenarioButtons.forEach((item) => {
        item.classList.toggle("active", item.dataset.scenario === state.scenario);
      });
      setActiveDemo(state.activeId);
    });
  });

  runBtn.addEventListener("click", () => {
    state.activeDemo?.run();
    draw();
    syncSidePanel();
  });
  pauseBtn.addEventListener("click", () => {
    state.activeDemo?.pause();
    syncSidePanel();
  });
  stepBtn.addEventListener("click", () => {
    state.activeDemo?.step();
    draw();
    syncSidePanel();
  });
  resetBtn.addEventListener("click", () => {
    state.activeDemo?.reset();
    draw();
    syncSidePanel();
  });
  trainingResetBtn.addEventListener("click", () => {
    state.activeDemo?.resetTraining?.();
    draw();
    syncSidePanel();
  });
  speedSlider.addEventListener("input", () => {
    state.speed = Number(speedSlider.value);
  });
  window.addEventListener("resize", resizeCanvas);

  setActiveDemo("search");
  resizeCanvas();
  requestAnimationFrame(loop);
})();
