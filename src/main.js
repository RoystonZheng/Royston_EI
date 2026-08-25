(() => {
  "use strict";

  const canvas = document.getElementById("labCanvas");
  const ctx = canvas.getContext("2d");
  const demoTitle = document.getElementById("demoTitle");
  const demoClaim = document.getElementById("demoClaim");
  const metricsEl = document.getElementById("metrics");
  const legendEl = document.getElementById("legend");
  const secondaryBtn = document.getElementById("secondaryBtn");
  const runBtn = document.getElementById("runBtn");
  const pauseBtn = document.getElementById("pauseBtn");
  const stepBtn = document.getElementById("stepBtn");
  const resetBtn = document.getElementById("resetBtn");
  const speedSlider = document.getElementById("speedSlider");
  const tabButtons = Array.from(document.querySelectorAll(".tab"));
  const difficultyButtons = Array.from(document.querySelectorAll(".difficulty"));

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
  };

  const DIFFICULTY = {
    normal: { label: "普通", multiplier: 1 },
    hard: { label: "困难", multiplier: 1.45 },
    extreme: { label: "极限", multiplier: 2.1 },
  };

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

  function splitBoxes(width, height, count) {
    const gap = 12;
    const margin = 14;
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

  function drawMetricText(box, lines) {
    ctx.font = "12px Inter, sans-serif";
    lines.forEach((line, i) => {
      ctx.fillStyle = i === 0 ? COLOR.text : COLOR.muted;
      ctx.fillText(line, box.x + 14, box.y + box.h - 18 - (lines.length - 1 - i) * 17);
    });
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

  function makeSearchMap(difficulty = "hard") {
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
    return { grid, w, h, start, goal };
  }

  function makeDynamicMap(difficulty = "hard") {
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
    return { grid, w, h, start, goal };
  }

  function makeCarMap(difficulty = "hard") {
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
    const { w, h, width, points } = config;
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
    return { grid, w, h, start: points[0], goal: points[points.length - 1], corridorWidth: width };
  }

  class SearchComparisonDemo {
    constructor(difficulty = "hard") {
      this.difficulty = difficulty;
      this.title = "Dijkstra vs A*";
      this.claim = "同一张静态地图里，Dijkstra 会像水一样扩散；A* 用目标方向做引导，通常少搜很多格子。";
      this.reset();
    }

    reset() {
      const map = makeSearchMap(this.difficulty);
      Object.assign(this, map);
      this.dijkstra = gridSearch({
        grid: this.grid,
        w: this.w,
        h: this.h,
        start: this.start,
        goal: this.goal,
        heuristic: () => 0,
      });
      this.astar = gridSearch({
        grid: this.grid,
        w: this.w,
        h: this.h,
        start: this.start,
        goal: this.goal,
        heuristic: manhattan,
      });
      this.progress = 0;
      this.running = false;
    }

    run() {
      this.running = true;
    }

    pause() {
      this.running = false;
    }

    step() {
      this.progress += 80;
    }

    update(dt, speed) {
      if (!this.running) return;
      this.progress += dt * (0.18 + speed * 0.075);
      const done = this.progress > Math.max(this.dijkstra.visitedOrder.length, this.astar.visitedOrder.length) + 60;
      if (done) this.running = false;
    }

    render(width, height) {
      const boxes = splitBoxes(width, height, 2);
      this.renderOne(boxes[0], "Dijkstra", this.dijkstra, COLOR.visited);
      this.renderOne(boxes[1], "A*", this.astar, COLOR.visitedAlt);
    }

    renderOne(box, label, result, visitedColor) {
      drawBox(box, label, label === "Dijkstra" ? "没有启发方向，按代价一圈圈扩散" : "用曼哈顿距离朝终点收缩搜索");
      const geom = getGridGeometry(box, this.w, this.h, 54);
      drawGridBase(this.grid, this.w, this.h, geom);
      drawVisited(result.visitedOrder, geom, visitedColor, this.progress, 0.28);
      if (this.progress >= result.visitedOrder.length) {
        drawGridPath(result.path, geom, COLOR.path, 3.2, 1);
      }
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
      const aVisited = Math.min(this.astar.visitedOrder.length, Math.floor(this.progress));
      const reduction = 1 - this.astar.visitedOrder.length / Math.max(1, this.dijkstra.visitedOrder.length);
      return [
        ["实验强度", DIFFICULTY[this.difficulty]?.label ?? this.difficulty],
        ["Dijkstra 搜索格子", `${dVisited}/${this.dijkstra.visitedOrder.length}`],
        ["A* 搜索格子", `${aVisited}/${this.astar.visitedOrder.length}`],
        ["A* 少搜索", `${formatNumber(reduction * 100, 1)}%`],
        ["两者路径长度", `${this.dijkstra.pathLength} / ${this.astar.pathLength}`],
        ["计算耗时", `${formatNumber(this.dijkstra.timeMs, 2)}ms / ${formatNumber(this.astar.timeMs, 2)}ms`],
      ];
    }

    legend() {
      return [
        ["障碍物", COLOR.obstacle],
        ["搜索留痕", COLOR.visited],
        ["另一侧搜索留痕", COLOR.visitedAlt],
        ["最终路径", COLOR.path],
        ["起点/终点", COLOR.start],
      ];
    }

    secondary() {
      return null;
    }
  }

  function makeDynamicObstacles(difficulty, w, h) {
    const midY = Math.floor(h / 2);
    const speedScale = DIFFICULTY[difficulty]?.multiplier ?? 1.45;
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
        period: difficulty === "extreme" ? 2.4 : 3.2,
        duty: difficulty === "normal" ? 0.42 : 0.58,
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

    return obstacles;
  }

  class DynamicAStarDemo {
    constructor(difficulty = "hard") {
      this.difficulty = difficulty;
      this.title = "A* 动态障碍";
      this.claim = "A* 规划的是当前地图。多个高速障碍按不同轨迹移动时，旧路径很快失效，只能反复重算。";
      this.reset();
    }

    reset() {
      const map = makeDynamicMap(this.difficulty);
      Object.assign(this, map);
      this.agent = { ...this.start };
      this.obstacles = makeDynamicObstacles(this.difficulty, this.w, this.h);
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
      this.timeSec = 0;
      this.agentAcc = 0;
      this.agentDelay = this.difficulty === "extreme" ? 95 : this.difficulty === "hard" ? 115 : 135;
      this.replan(true);
    }

    run() {
      this.running = true;
    }

    pause() {
      this.running = false;
    }

    step() {
      const wasRunning = this.running;
      this.running = true;
      this.update(180, 5);
      this.running = wasRunning;
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

        if (obstacle.kind === "gate") {
          const cycle = (this.timeSec + obstacle.phase) % obstacle.period;
          obstacle.active = cycle < obstacle.period * obstacle.duty;
        }
      });
    }

    maxObstacleSpeed() {
      return this.obstacles.reduce((max, obstacle) => Math.max(max, obstacle.speed || 0), 0);
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
        extraBlocked: (x, y) => this.isDynamicCell(x, y),
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
        if (this.isDynamicCell(p.x, p.y)) return true;
      }
      return false;
    }

    moveAgent() {
      if (this.reached) return;
      if (this.isDynamicCell(this.agent.x, this.agent.y)) {
        this.nearMisses += 1;
        this.replan(false);
        return;
      }
      if (!this.currentPath.length || this.pathIndex >= this.currentPath.length - 1) {
        if (this.agent.x === this.goal.x && this.agent.y === this.goal.y) {
          this.reached = true;
          this.running = false;
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
      if (this.isDynamicCell(next.x, next.y)) {
        this.blockedEvents += 1;
        this.replan(false);
        return;
      }
      this.agent = { ...next };
      this.pathIndex += 1;
      if (this.agent.x === this.goal.x && this.agent.y === this.goal.y) {
        this.reached = true;
        this.running = false;
      }
    }

    update(dt, speed) {
      const visibleSpeed = Math.max(1, speed);
      this.searchLayers.forEach((layer) => {
        layer.progress = Math.min(layer.visitedOrder.length, layer.progress + dt * (0.2 + visibleSpeed * 0.08));
      });
      if (!this.running || this.reached) return;

      this.moveDynamicObstacles(dt, visibleSpeed);
      this.agentAcc += dt * (visibleSpeed / 5);

      while (this.agentAcc >= this.agentDelay) {
        this.moveAgent();
        this.agentAcc -= this.agentDelay;
      }
    }

    render(width, height) {
      const box = { x: 14, y: 14, w: width - 28, h: height - 28 };
      drawBox(box, "A* 动态障碍", "橙色障碍按横向、纵向、矩形和开关门轨迹移动");
      const geom = getGridGeometry(box, this.w, this.h, 54);
      drawGridBase(this.grid, this.w, this.h, geom);
      this.drawDynamicRoutes(geom);

      this.searchLayers.forEach((layer, indexValue) => {
        const isLast = indexValue === this.searchLayers.length - 1;
        drawVisited(layer.visitedOrder, geom, isLast ? COLOR.visited : COLOR.visitedAlt, isLast ? layer.progress : layer.visitedOrder.length, isLast ? 0.24 : 0.08);
      });
      this.oldPaths.forEach((path) => drawGridPath(path, geom, COLOR.oldPath, 2, 0.42));
      drawGridPath(this.currentPath.slice(this.pathIndex), geom, COLOR.path, 3, 1);

      this.drawDynamicObstacles(geom);

      drawMarker(this.agent, geom, COLOR.start, "R");
      drawMarker(this.goal, geom, COLOR.goal, "G");
      drawMetricText(box, [
        this.reached ? "状态: 已到达终点" : "状态: 路径跟随中",
        `规划调用 ${this.planCalls} | 重新规划 ${this.replans} | 失效 ${this.blockedEvents} | 等待 ${this.waitEvents}`,
      ]);
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
        ["动态障碍数", this.obstacles.length],
        ["最快障碍速度", `${formatNumber(this.maxObstacleSpeed(), 1)} 格/秒`],
        ["规划调用", this.planCalls],
        ["重新规划次数", this.replans],
        ["路径失效次数", this.blockedEvents],
        ["等待/无路次数", this.waitEvents],
        ["贴脸风险次数", this.nearMisses],
        ["累计搜索格子", this.totalVisited],
        ["平均每次搜索", `${formatNumber(avgVisited, 0)} 格`],
        ["最近一次搜索", latest ? `${latest.visitedOrder.length} 格 / ${formatNumber(latest.timeMs, 2)}ms` : "-"],
        ["状态", this.reached ? "已到达" : this.running ? "运行中" : "暂停"],
      ];
    }

    legend() {
      return [
        ["静态障碍", COLOR.obstacle],
        ["动态障碍", COLOR.dynamic],
        ["障碍运动轨迹", COLOR.dynamic],
        ["搜索留痕", COLOR.visited],
        ["旧路径", COLOR.oldPath],
        ["当前路径", COLOR.path],
        ["机器人/目标", COLOR.start],
      ];
    }

    secondary() {
      return null;
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

  function segmentBlocked(a, b, rects, areaW, areaH) {
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const length = Math.hypot(dx, dy);
    const steps = Math.max(2, Math.ceil(length / 5));
    for (let i = 0; i <= steps; i += 1) {
      const t = i / steps;
      const p = { x: lerp(a.x, b.x, t), y: lerp(a.y, b.y, t) };
      if (p.x < 0 || p.y < 0 || p.x > areaW || p.y > areaH) return true;
      for (const rect of rects) {
        if (pointInRect(p, rect)) return true;
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

  class RRTDemo {
    constructor(difficulty = "hard") {
      this.difficulty = difficulty;
      this.title = "RRT 窄通道";
      this.claim = "RRT 靠随机采样长树。窄门太小的时候，采样不一定打中门口，所以同一个任务每次结果都可能不同。";
      this.areaW = 1000;
      this.areaH = 560;
      this.reset();
    }

    reset() {
      const config = {
        normal: { gap: 44, maxIterations: 1600, stepSize: 20, goalRadius: 28, goalBias: 0.07 },
        hard: { gap: 30, maxIterations: 2200, stepSize: 17, goalRadius: 24, goalBias: 0.05 },
        extreme: { gap: 22, maxIterations: 3000, stepSize: 14, goalRadius: 20, goalBias: 0.035 },
      }[this.difficulty] ?? { gap: 30, maxIterations: 2200, stepSize: 17, goalRadius: 24, goalBias: 0.05 };
      const { gap } = config;
      const mid = this.areaH / 2;
      this.gap = gap;
      this.rects = [
        { x: 482, y: 0, w: 46, h: mid - gap / 2 },
        { x: 482, y: mid + gap / 2, w: 46, h: mid - gap / 2 },
        { x: 675, y: 72, w: 86, h: 170 },
        { x: 690, y: 320, w: 90, h: 150 },
      ];
      if (this.difficulty !== "normal") {
        this.rects.push(
          { x: 250, y: 70, w: 78, h: 156 },
          { x: 245, y: 338, w: 95, h: 120 },
          { x: 572, y: 214, w: 80, h: 40 },
        );
      }
      if (this.difficulty === "extreme") {
        this.rects.push(
          { x: 360, y: 248, w: 74, h: 28 },
          { x: 808, y: 188, w: 64, h: 160 },
        );
      }
      this.start = { x: 75, y: 280 };
      this.goal = { x: 930, y: 280 };
      this.nodes = [{ ...this.start, parent: -1 }];
      this.iterations = 0;
      this.maxIterations = config.maxIterations;
      this.stepSize = config.stepSize;
      this.goalRadius = config.goalRadius;
      this.goalBias = config.goalBias;
      this.finalPath = [];
      this.success = false;
      this.failed = false;
      this.running = false;
      this.rng = mulberry32((Date.now() & 0xffffffff) || 1);
      this.lastTrialSummary = "还没跑";
    }

    run() {
      if (this.success || this.failed) this.reset();
      this.running = true;
    }

    pause() {
      this.running = false;
    }

    step() {
      for (let i = 0; i < 8; i += 1) this.extendTree();
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
      if (!segmentBlocked(nearest, next, this.rects, this.areaW, this.areaH)) {
        this.nodes.push(next);
        if (Math.hypot(next.x - this.goal.x, next.y - this.goal.y) <= this.goalRadius && !segmentBlocked(next, this.goal, this.rects, this.areaW, this.areaH)) {
          this.nodes.push({ ...this.goal, parent: this.nodes.length - 1 });
          this.success = true;
          this.running = false;
          this.finalPath = this.tracePath(this.nodes.length - 1);
        }
      }
      if (this.iterations >= this.maxIterations && !this.success) {
        this.failed = true;
        this.running = false;
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
      if (!this.running) return;
      const iterationsThisFrame = Math.max(1, Math.floor(speed * 2 + dt * 0.025 * speed));
      for (let i = 0; i < iterationsThisFrame; i += 1) this.extendTree();
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
        if (segmentBlocked(nearest, next, this.rects, this.areaW, this.areaH)) continue;
        nodes.push(next);
        if (Math.hypot(next.x - this.goal.x, next.y - this.goal.y) <= this.goalRadius && !segmentBlocked(next, this.goal, this.rects, this.areaW, this.areaH)) {
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
      const box = { x: 14, y: 14, w: width - 28, h: height - 28 };
      drawBox(box, "RRT 窄通道", "树枝是随机采样留下的搜索痕迹，窄门越小越看运气");
      const fit = fitAreaToBox(box, this.areaW, this.areaH, 54);
      const sx = (x) => fit.x + x * fit.scale;
      const sy = (y) => fit.y + y * fit.scale;

      ctx.fillStyle = "#0b0f15";
      ctx.fillRect(fit.x, fit.y, fit.w, fit.h);
      ctx.strokeStyle = "#566276";
      ctx.strokeRect(fit.x, fit.y, fit.w, fit.h);

      ctx.fillStyle = COLOR.obstacle;
      this.rects.forEach((rect) => {
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

      drawCircle(sx(this.start.x), sy(this.start.y), 9, COLOR.start, "S");
      drawCircle(sx(this.goal.x), sy(this.goal.y), 11, COLOR.goal, "G");
      drawMetricText(box, [
        this.success ? "状态: 成功穿过窄门" : this.failed ? "状态: 本轮失败" : "状态: 随机采样中",
        `迭代 ${this.iterations}/${this.maxIterations} | 节点 ${this.nodes.length} | ${this.lastTrialSummary}`,
      ]);
    }

    metrics() {
      const pathLength = this.finalPath.length ? pathLengthContinuous(this.finalPath) : 0;
      return [
        ["实验强度", DIFFICULTY[this.difficulty]?.label ?? this.difficulty],
        ["窄门宽度", `${this.gap} px`],
        ["当前状态", this.success ? "成功" : this.failed ? "失败" : this.running ? "运行中" : "暂停"],
        ["迭代次数", `${this.iterations}/${this.maxIterations}`],
        ["树节点数", this.nodes.length],
        ["最终路径长度", pathLength ? formatNumber(pathLength, 1) : "-"],
        ["20 次快速试验", this.lastTrialSummary],
      ];
    }

    legend() {
      return [
        ["障碍物", COLOR.obstacle],
        ["RRT 搜索树", COLOR.visited],
        ["找到的路径", COLOR.path],
        ["起点/终点", COLOR.start],
      ];
    }

    secondary() {
      return {
        label: "快速跑 20 次",
        action: () => this.runTrials(),
      };
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
    constructor(difficulty = "hard") {
      this.difficulty = difficulty;
      this.title = "网格路径 vs 小车";
      this.claim = "A* 给的是格子路线，不知道小车有转弯半径。直角路线看起来最短，真实小车可能跟不上。";
      this.reset();
    }

    reset() {
      const map = makeCarMap(this.difficulty);
      Object.assign(this, map);
      this.searchResult = gridSearch({
        grid: this.grid,
        w: this.w,
        h: this.h,
        start: this.start,
        goal: this.goal,
        heuristic: manhattan,
      });
      this.path = this.searchResult.path;
      this.centers = this.path.map(cellCenter);
      this.pose = { x: this.start.x + 0.5, y: this.start.y + 0.5, theta: 0 };
      this.waypoint = 1;
      this.trail = [{ x: this.pose.x, y: this.pose.y }];
      this.running = false;
      this.reached = false;
      this.collided = false;
      this.collisionPoint = null;
      this.maxError = 0;
      this.sharpTurns = countTurns(this.path);
      this.carRadius = this.difficulty === "extreme" ? 0.55 : 0.48;
      this.carSpeed = this.difficulty === "normal" ? 0.17 : this.difficulty === "hard" ? 0.2 : 0.23;
      this.maxTurn = this.difficulty === "normal" ? 0.03 : this.difficulty === "hard" ? 0.023 : 0.018;
    }

    run() {
      if (this.reached || this.collided) this.reset();
      this.running = true;
    }

    pause() {
      this.running = false;
    }

    step() {
      const wasRunning = this.running;
      this.running = true;
      for (let i = 0; i < 8; i += 1) this.advance();
      this.running = wasRunning;
    }

    collidesAt(x, y) {
      const samples = [{ x, y }];
      for (let i = 0; i < 12; i += 1) {
        const angle = (Math.PI * 2 * i) / 12;
        samples.push({
          x: x + Math.cos(angle) * this.carRadius,
          y: y + Math.sin(angle) * this.carRadius,
        });
      }
      return samples.some((sample) => {
        const cx = Math.floor(sample.x);
        const cy = Math.floor(sample.y);
        return isGridBlocked(this.grid, this.w, this.h, cx, cy);
      });
    }

    advance() {
      if (!this.running || this.reached || this.collided || this.centers.length < 2) return;
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
      this.pose.theta += clamp(delta, -this.maxTurn, this.maxTurn);
      const nextX = this.pose.x + Math.cos(this.pose.theta) * this.carSpeed;
      const nextY = this.pose.y + Math.sin(this.pose.theta) * this.carSpeed;
      this.pose.x = nextX;
      this.pose.y = nextY;
      this.trail.push({ x: nextX, y: nextY });
      if (this.trail.length > 1600) this.trail.shift();
      this.maxError = Math.max(this.maxError, distanceToCenters(this.pose, this.centers));

      if (this.collidesAt(nextX, nextY)) {
        this.collided = true;
        this.running = false;
        this.collisionPoint = { x: nextX, y: nextY };
        return;
      }
      const goalCenter = cellCenter(this.goal);
      if (Math.hypot(nextX - goalCenter.x, nextY - goalCenter.y) < 0.72) {
        this.reached = true;
        this.running = false;
      }
    }

    update(dt, speed) {
      if (!this.running) return;
      const steps = Math.max(1, Math.floor(speed * 1.4 + dt * 0.018 * speed));
      for (let i = 0; i < steps; i += 1) this.advance();
    }

    render(width, height) {
      const box = { x: 14, y: 14, w: width - 28, h: height - 28 };
      drawBox(box, "网格路径 vs 小车", "黄色是网格路径，青色是带转弯限制的小车轨迹");
      const geom = getGridGeometry(box, this.w, this.h, 54);
      drawGridBase(this.grid, this.w, this.h, geom);
      drawVisited(this.searchResult.visitedOrder, geom, COLOR.visited, this.searchResult.visitedOrder.length, 0.12);
      drawGridPath(this.path, geom, COLOR.path, 3, 1);
      this.drawTrail(geom);
      this.drawCar(geom);
      if (this.collisionPoint) this.drawCollision(geom);
      drawMarker(this.start, geom, COLOR.start, "S");
      drawMarker(this.goal, geom, COLOR.goal, "G");
      drawMetricText(box, [
        this.collided ? "状态: 小车在直角处撞墙" : this.reached ? "状态: 已到达" : "状态: 正在跟踪 A* 路径",
        `A* 转弯 ${this.sharpTurns} 次 | 最大偏离 ${formatNumber(this.maxError, 2)} 格 | 搜索格子 ${this.searchResult.visitedOrder.length}`,
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

    drawCar(geom) {
      const cx = geom.x + this.pose.x * geom.cell;
      const cy = geom.y + this.pose.y * geom.cell;
      const size = Math.max(8, geom.cell * 0.9);
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(this.pose.theta);
      ctx.fillStyle = this.collided ? COLOR.failure : COLOR.actual;
      ctx.beginPath();
      ctx.moveTo(size * 0.75, 0);
      ctx.lineTo(-size * 0.55, -size * 0.42);
      ctx.lineTo(-size * 0.35, 0);
      ctx.lineTo(-size * 0.55, size * 0.42);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
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
        ["走廊宽度", `${this.corridorWidth} 格`],
        ["A* 搜索格子", this.searchResult.visitedOrder.length],
        ["A* 路径长度", this.searchResult.pathLength],
        ["直角转弯次数", this.sharpTurns],
        ["最大跟踪偏离", `${formatNumber(this.maxError, 2)} 格`],
        ["小车状态", this.collided ? "撞墙" : this.reached ? "到达" : this.running ? "运行中" : "暂停"],
      ];
    }

    legend() {
      return [
        ["墙体", COLOR.obstacle],
        ["A* 搜索留痕", COLOR.visited],
        ["A* 网格路径", COLOR.path],
        ["小车真实轨迹", COLOR.actual],
        ["碰撞点", COLOR.failure],
      ];
    }

    secondary() {
      return null;
    }
  }

  const factories = {
    search: () => new SearchComparisonDemo(state.difficulty),
    dynamic: () => new DynamicAStarDemo(state.difficulty),
    rrt: () => new RRTDemo(state.difficulty),
    car: () => new CarTrackingDemo(state.difficulty),
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

  runBtn.addEventListener("click", () => state.activeDemo?.run());
  pauseBtn.addEventListener("click", () => state.activeDemo?.pause());
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
  speedSlider.addEventListener("input", () => {
    state.speed = Number(speedSlider.value);
  });
  window.addEventListener("resize", resizeCanvas);

  setActiveDemo("search");
  resizeCanvas();
  requestAnimationFrame(loop);
})();
