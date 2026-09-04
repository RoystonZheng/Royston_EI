const fs = require("fs");
const path = require("path");

const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "index.html"), "utf8");
const rolloutCode = fs.readFileSync(path.join(root, "src/ai_rollouts.js"), "utf8");
const mainCode = fs.readFileSync(path.join(root, "src/main.js"), "utf8");

class Element {
  constructor(id = "", dataset = {}) {
    this.id = id;
    this.dataset = dataset;
    this.children = [];
    this.style = {};
    this.hidden = false;
    this.textContent = "";
    this.onclick = null;
    this.className = "";
    this.listeners = {};
    this.classList = { toggle: () => {} };
  }

  append(...items) {
    this.children.push(...items);
  }

  replaceChildren(...items) {
    this.children = [...items];
  }

  addEventListener(type, handler) {
    this.listeners[type] = handler;
  }

  click() {
    if (this.listeners.click) this.listeners.click();
    if (this.onclick) this.onclick();
  }

  getBoundingClientRect() {
    return { width: 1200, height: 720 };
  }

  getContext() {
    return ctx;
  }
}

const ctx = new Proxy(
  {},
  {
    get(target, prop) {
      if (prop in target) return target[prop];
      if (prop === "measureText") return (text) => ({ width: String(text).length * 7 });
      return () => {};
    },
    set(target, prop, value) {
      target[prop] = value;
      return true;
    },
  },
);

function collectText(node) {
  if (!node) return "";
  const own = node.textContent || "";
  const childText = (node.children || []).map(collectText).join(" ");
  return `${own} ${childText}`.trim();
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function metricsMap(metricsNode) {
  const values = {};
  for (const row of metricsNode.children || []) {
    const label = row.children?.[0]?.textContent;
    const value = row.children?.[1]?.textContent;
    if (label) values[label] = value;
  }
  return values;
}

function firstNumber(text) {
  const match = String(text || "").match(/-?\d+(?:\.\d+)?/);
  return match ? Number(match[0]) : 0;
}

assert(html.includes('id="trainingResetBtn"'), "index.html missing reset training button");

const ids = [
  "labCanvas",
  "demoTitle",
  "demoClaim",
  "metrics",
  "drawbacks",
  "evaluation",
  "legend",
  "secondaryBtn",
  "runBtn",
  "pauseBtn",
  "stepBtn",
  "resetBtn",
  "trainingResetBtn",
  "speedSlider",
];
const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
elements.speedSlider.value = "5";

const tabs = ["search", "dynamic", "rrt", "car"].map((demo) => new Element("", { demo }));
const difficulties = ["normal", "hard", "extreme"].map((difficulty) => new Element("", { difficulty }));
const scenarios = ["baseline", "perturbed", "generalization"].map((scenario) => new Element("", { scenario }));

const document = {
  getElementById(id) {
    return elements[id];
  },
  querySelectorAll(selector) {
    if (selector === ".tab") return tabs;
    if (selector === ".difficulty") return difficulties;
    if (selector === ".scenario") return scenarios;
    return [];
  },
  createElement() {
    return new Element();
  },
  createTextNode(text) {
    return { textContent: text, children: [] };
  },
};

const window = { document, devicePixelRatio: 1, addEventListener() {} };
global.window = window;
global.document = document;
global.performance = { now: () => Date.now() };
global.requestAnimationFrame = () => 0;

Function("window", "document", `${rolloutCode}\n${mainCode}`)(window, document);

const requiredMetricByDemo = {
  search: ["机器人半径", "地图编号", "移动障碍数", "AI 训练轮次"],
  dynamic: ["机器人半径", "地图编号", "移动障碍数", "AI 训练轮次"],
  rrt: ["机器人安全半径", "地图编号", "移动障碍数", "AI 训练轮次"],
  car: ["车体尺寸", "最小转弯半径", "地图编号", "移动障碍数", "AI 训练轮次"],
};

let combos = 0;
for (const difficultyButton of difficulties) {
  difficultyButton.click();
  for (const scenarioButton of scenarios) {
    scenarioButton.click();
    for (const tabButton of tabs) {
      tabButton.click();
      const demo = tabButton.dataset.demo;
      const combo = `${demo}/${difficultyButton.dataset.difficulty}/${scenarioButton.dataset.scenario}`;
      elements.runBtn.click();
      const afterFirstRun = collectText(elements.metrics);
      const firstRunMetrics = metricsMap(elements.metrics);
      elements.runBtn.click();
      const afterSecondRun = collectText(elements.metrics);
      const secondRunMetrics = metricsMap(elements.metrics);
      assert(afterFirstRun !== afterSecondRun, `${combo} run did not refresh map/train state`);
      assert(firstRunMetrics["地图编号"] !== secondRunMetrics["地图编号"], `${combo} map id did not change on run`);
      assert(!afterSecondRun.includes("训练轮次 0"), `${combo} did not train AI on run`);

      elements.trainingResetBtn.click();
      const afterTrainingReset = collectText(elements.metrics);
      assert(afterTrainingReset.includes("AI 训练轮次 0"), `${combo} reset training did not clear AI training`);

      const beforeStep = collectText(elements.metrics);
      elements.stepBtn.click();
      elements.stepBtn.click();
      const afterStep = collectText(elements.metrics);
      const metricsText = collectText(elements.metrics);
      const metrics = metricsMap(elements.metrics);
      const evaluationText = collectText(elements.evaluation);
      const legendText = collectText(elements.legend);

      assert(metricsText.includes("环境模式"), `${combo} missing scenario metric`);
      assert(evaluationText.includes("鲁棒性"), `${combo} missing robustness note`);
      assert(evaluationText.includes("泛化性"), `${combo} missing generalization note`);
      assert(evaluationText.includes("可解释性"), `${combo} missing explainability note`);
      assert(metricsText.includes("传统状态"), `${combo} missing independent traditional status`);
      assert(metricsText.includes("AI 状态"), `${combo} missing independent AI status`);
      assert(beforeStep !== afterStep, `${combo} did not update metrics after stepping`);
      for (const required of requiredMetricByDemo[demo]) {
        assert(metricsText.includes(required), `${combo} missing required metric: ${required}`);
      }
      if (demo === "search") {
        assert(firstNumber(metrics["两者路径长度"]) > 0, `${combo} generated a blocked Dijkstra map`);
      }
      if (demo === "car") {
        assert(firstNumber(metrics["A* 路径长度"]) > 0, `${combo} generated a blocked car map`);
      }
      if (demo === "rrt") {
        assert(firstNumber(metrics["窄门宽度"]) > firstNumber(metrics["机器人安全半径"]) * 2, `${combo} generated an impossible RRT gate`);
      }
      assert(/车体|机器人|安全边界/.test(legendText), `${combo} legend missing body/robot footprint cue`);
      combos += 1;
    }
  }
}

console.log(`ui smoke ok: ${combos} combinations`);
