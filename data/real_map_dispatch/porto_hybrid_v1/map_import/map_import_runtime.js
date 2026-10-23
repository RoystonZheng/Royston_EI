(() => {
  const root = document.getElementById("porto-map-v1");
  const d3 = window.d3;
  const data = JSON.parse(root.querySelector("[data-map-data]").textContent);
  const accessStatuses = ["candidate", "outscope", "review", "blocked"];
  const decodePolyline = encoded => {
    let index = 0, lat = 0, lon = 0;
    const coordinates = [];
    const readDelta = () => {
      let result = 0, shift = 0, byte;
      do {
        byte = encoded.charCodeAt(index++) - 63;
        result |= (byte & 0x1f) << shift;
        shift += 5;
      } while (byte >= 0x20);
      return result & 1 ? ~(result >> 1) : result >> 1;
    };
    while (index < encoded.length) {
      lat += readDelta(); lon += readDelta();
      coordinates.push([lon / 1e6, lat / 1e6]);
    }
    return coordinates;
  };
  data.streets = {
    type: "FeatureCollection",
    features: data.rawRoadsPacked.map(([wayId, classIndex, statusIndex, direction, reasonText, encoded]) => {
      const accessReasons = reasonText ? reasonText.split("|") : [];
      const accessStatus = accessStatuses[statusIndex];
      return {
        type: "Feature", id: wayId,
        properties: { wayId, class: data.roadClasses[classIndex], accessStatus, routingCandidate: accessStatus === "candidate", direction, accessReasons },
        geometry: { type: "LineString", coordinates: decodePolyline(encoded) }
      };
    })
  };
  delete data.rawRoadsPacked;
  delete data.roadClasses;

  const projection = d3.geoMercator().fitExtent([[34, 26], [926, 594]], data.streets);
  const geoPath = d3.geoPath(projection);
  const groups = new Map([...root.querySelectorAll("[data-map-layer]")].map(node => [node.dataset.mapLayer, d3.select(node)]));
  const project = point => projection([point[0], point[1]]);
  const distanceMeters = (a, b) => {
    const lat1 = a[1] * Math.PI / 180, lat2 = b[1] * Math.PI / 180;
    const dLat = lat2 - lat1, dLon = (b[0] - a[0]) * Math.PI / 180;
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(lat1) * Math.cos(lat2) * Math.sin(dLon / 2) ** 2;
    return 6371000 * 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h));
  };
  const coordinateKey = point => `${Math.round(point[0] * 1e6)},${Math.round(point[1] * 1e6)}`;
  const projectSegmentMeters = (point, segment) => {
    const cosLat = Math.cos(point[1] * Math.PI / 180);
    const xy = coordinate => [(coordinate[0] - point[0]) * 111320 * cosLat, (coordinate[1] - point[1]) * 111320];
    const [ax, ay] = xy(segment.a), [bx, by] = xy(segment.b);
    const vx = bx - ax, vy = by - ay;
    const ratio = Math.max(0, Math.min(1, -(ax * vx + ay * vy) / (vx * vx + vy * vy || 1)));
    return { ratio, distance: Math.hypot(ax + ratio * vx, ay + ratio * vy) };
  };

  const roadFeatures = data.streets.features;
  const routingRoads = roadFeatures.filter(feature => feature.properties.routingCandidate);
  const nodes = new Map();
  const nodeList = [];
  const physicalSegments = [];
  const getNode = coordinate => {
    const key = coordinateKey(coordinate);
    if (!nodes.has(key)) {
      const node = { key, coordinate, edges: [] };
      nodes.set(key, node); nodeList.push(node);
    }
    return nodes.get(key);
  };
  const addDirectedEdge = (segment, from, to, coordinates, suffix) => {
    const edge = { id: `${segment.id}:${suffix}`, from: from.key, to: to.key, segmentId: segment.id, length: segment.length, wayId: segment.wayId, coordinates };
    from.edges.push(edge);
    return edge;
  };
  for (const [wayIndex, feature] of roadFeatures.entries()) {
    if (!feature.properties.routingCandidate) continue;
    const coords = feature.geometry.coordinates;
    for (let i = 1; i < coords.length; i++) {
      const a = coords[i - 1], b = coords[i], length = distanceMeters(a, b);
      if (length < 0.5) continue;
      const segment = {
        id: physicalSegments.length,
        physicalKey: `${feature.properties.wayId}:${i - 1}`,
        wayId: feature.properties.wayId,
        wayIndex,
        segmentIndex: i - 1,
        roadClass: feature.properties.class,
        direction: feature.properties.direction,
        a, b, length,
        signalIndexes: [],
        demandCount: 0,
        linkUseCount: 0
      };
      const aNode = getNode(a), bNode = getNode(b);
      if (segment.direction >= 0) addDirectedEdge(segment, aNode, bNode, [a, b], "f");
      if (segment.direction <= 0) addDirectedEdge(segment, bNode, aNode, [b, a], "r");
      physicalSegments.push(segment);
    }
  }

  const signalFeatures = data.signals.features;
  for (let signalIndex = 0; signalIndex < signalFeatures.length; signalIndex++) {
    const coordinate = signalFeatures[signalIndex].geometry.coordinates;
    let closest = null;
    for (const segment of physicalSegments) {
      const projected = projectSegmentMeters(coordinate, segment);
      if (!closest || projected.distance < closest.distance) closest = { segment, distance: projected.distance };
    }
    if (closest && closest.distance <= 30) closest.segment.signalIndexes.push(signalIndex);
  }
  const nearestNodeCache = new Map();
  function nearestNode(coordinate, maxDistance = 300) {
    const key = coordinateKey(coordinate);
    if (nearestNodeCache.has(key)) return nearestNodeCache.get(key);
    let best = null;
    for (const node of nodeList) {
      const distance = distanceMeters(coordinate, node.coordinate);
      if (!best || distance < best.distance) best = { node, distance };
    }
    const result = best && best.distance <= maxDistance ? best : null;
    nearestNodeCache.set(key, result);
    return result;
  }

  class MinHeap {
    constructor() { this.items = []; }
    get size() { return this.items.length; }
    push(item) {
      const a = this.items; a.push(item); let i = a.length - 1;
      while (i > 0) { const p = (i - 1) >> 1; if (a[p].distance <= item.distance) break; a[i] = a[p]; i = p; }
      a[i] = item;
    }
    pop() {
      const a = this.items;
      if (!a.length) return null;
      const first = a[0], last = a.pop();
      if (a.length) {
        let i = 0;
        while (true) {
          const left = i * 2 + 1, right = left + 1;
          if (left >= a.length) break;
          const child = right < a.length && a[right].distance < a[left].distance ? right : left;
          if (a[child].distance >= last.distance) break;
          a[i] = a[child]; i = child;
        }
        a[i] = last;
      }
      return first;
    }
  }
  function dijkstra(sourceKey, targetKeys = null) {
    const distances = new Map([[sourceKey, 0]]);
    const previous = new Map();
    const heap = new MinHeap();
    const remaining = targetKeys ? new Set([...targetKeys].filter(key => key !== sourceKey)) : null;
    if (remaining && remaining.size === 0) return { sourceKey, distances, previous };
    heap.push({ key: sourceKey, distance: 0 });
    while (heap.size) {
      const current = heap.pop();
      if (current.distance !== distances.get(current.key)) continue;
      if (remaining && remaining.delete(current.key) && remaining.size === 0) break;
      const node = nodes.get(current.key);
      if (!node) continue;
      for (const edge of node.edges) {
        const next = current.distance + edge.length;
        if (next < (distances.get(edge.to) ?? Infinity)) {
          distances.set(edge.to, next); previous.set(edge.to, edge);
          heap.push({ key: edge.to, distance: next });
        }
      }
    }
    return { sourceKey, distances, previous };
  }
  function recoverPath(result, destinationKey) {
    if (destinationKey === result.sourceKey) return [];
    const path = [];
    let current = destinationKey;
    while (current !== result.sourceKey) {
      const edge = result.previous.get(current);
      if (!edge) return null;
      path.push(edge); current = edge.from;
      if (path.length > nodeList.length) return null;
    }
    return path.reverse();
  }
  const yieldToUI = () => new Promise(resolve => requestAnimationFrame(resolve));

  // The raw road map is permanent; all demand-derived layers start empty.
  groups.get("roads").selectAll("path")
    .data(roadFeatures).join("path")
    .attr("d", geoPath).attr("class", "road road-base")
    .attr("data-tooltip", feature => `原始 OSM Way ${feature.properties.wayId} · highway=${feature.properties.class}`);
  groups.get("blocked").selectAll("path")
    .data(roadFeatures.filter(feature => !feature.properties.routingCandidate)).join("path")
    .attr("d", geoPath).attr("class", "road road-blocked")
    .attr("data-tooltip", feature => `本实验机动车路由图不纳入 · OSM Way ${feature.properties.wayId} · ${feature.properties.accessReasons.join(", ") || "道路类别或准入规则"}`);
  groups.get("oneway-roads").selectAll("path")
    .data(roadFeatures.filter(feature => feature.properties.direction !== 0)).join("path")
    .attr("d", geoPath).attr("class", "road road-oneway")
    .attr("data-tooltip", feature => `OSM 单行 Way ${feature.properties.wayId} · ${feature.properties.direction > 0 ? "按 Way 顺序" : "按 Way 逆序"}`);
  groups.get("fence").selectAll("path")
    .data(data.fence.features.map(feature => ({ ...feature, geometry: { type: "LineString", coordinates: feature.geometry.coordinates[0] } }))).join("path")
    .attr("d", geoPath).attr("class", "fence-line");
  groups.get("signals").selectAll("circle")
    .data(signalFeatures).join("circle")
    .attr("cx", feature => project(feature.geometry.coordinates)[0])
    .attr("cy", feature => project(feature.geometry.coordinates)[1])
    .attr("r", 2.6).attr("class", "signal-point signal-neutral")
    .attr("data-tooltip", "OSM 信号灯位置；相位需由导入包后的仿真时钟估算，非实测实时数据");

  const arrowData = [];
  for (const feature of roadFeatures.filter(item => item.properties.direction !== 0)) {
    let coords = feature.geometry.coordinates;
    if (feature.properties.direction < 0) coords = [...coords].reverse();
    const points = coords.map(project);
    let total = 0; const lengths = [];
    for (let i = 1; i < points.length; i++) {
      const length = Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
      lengths.push(length); total += length;
    }
    if (total < 38) continue;
    let remaining = total / 2, segmentIndex = 0;
    while (segmentIndex < lengths.length - 1 && remaining > lengths[segmentIndex]) remaining -= lengths[segmentIndex++];
    const length = lengths[segmentIndex] || 1, a = points[segmentIndex], b = points[segmentIndex + 1];
    const ratio = remaining / length;
    arrowData.push({ x: a[0] + (b[0] - a[0]) * ratio, y: a[1] + (b[1] - a[1]) * ratio, angle: Math.atan2(b[1] - a[1], b[0] - a[0]) * 180 / Math.PI + 90 });
  }
  groups.get("oneway-arrows").selectAll("path")
    .data(arrowData).join("path").attr("d", "M0,-4.6 L3.4,3 L-3.4,3 Z")
    .attr("transform", item => `translate(${item.x},${item.y}) rotate(${item.angle})`)
    .attr("class", "oneway-arrow");

  const layerInputs = [...root.querySelectorAll("input[data-layer]")];
  const initiallyUnavailable = new Set(["pickups", "dropoffs", "candidates", "links", "congestion"]);
  for (const input of layerInputs) {
    if (initiallyUnavailable.has(input.dataset.layer)) input.disabled = true;
    const targetLayers = input.dataset.layer === "oneway" ? ["oneway-roads", "oneway-arrows"] : [input.dataset.layer];
    for (const layerName of targetLayers) {
      const group = groups.get(layerName);
      if (group) group.attr("display", input.checked ? null : "none");
    }
    input.addEventListener("change", () => {
      for (const layerName of targetLayers) {
        const target = groups.get(layerName);
        if (target) target.attr("display", input.checked ? null : "none");
      }
    });
  }

  const baseCounts = data.counts;
  const mapCounts = root.querySelector("#porto-map-counts");
  mapCounts.textContent = `原始道路 ${baseCounts.rawHighwayWays.toLocaleString()} 条 OSM highway Way · 可路由候选道路 ${baseCounts.routingCandidateRoads.toLocaleString()} 条 · OSM 信号灯 ${signalFeatures.length.toLocaleString()} 处 · 尚未导入需求数据包`;
  const packageStatus = root.querySelector("#package-status");
  const packageSummary = root.querySelector("#package-summary");
  const candidateSummary = root.querySelector("#candidate-summary");
  const calculationSummary = root.querySelector("#calculation-summary");
  const fileInput = root.querySelector("#demand-package-file");
  const topNInput = root.querySelector("#candidate-top-n");
  const thresholdInput = root.querySelector("#candidate-threshold");
  const timeSlider = root.querySelector("#traffic-time");
  const sim = { time: 0, timer: null };
  const cycle = 60, greenSeconds = 32, yellowSeconds = 3, redSeconds = cycle - greenSeconds - yellowSeconds;
  let activePackage = null;
  let selectedCandidates = [];
  let demandPairs = [];
  let loadBySegment = new Map();
  let trafficMetrics = [];
  let candidatePairCount = 0;
  let candidatePairReachable = 0;
  let unroutedPairCount = 0;
  let routedDemand = 0;
  let totalDemand = 0;
  let maxLinkUse = 1;
  let maxRoadLoad = 1;
  let peakSnapFailures = 0;
  let dropoffSnapFailures = 0;
  let sameNodePairCount = 0;
  let sameNodeDemand = 0;
  const maxSnapMeters = 300;
  const routePalette = {
    green: getComputedStyle(root).getPropertyValue("--green").trim() || "green",
    yellow: getComputedStyle(root).getPropertyValue("--yellow").trim() || "orange",
    red: getComputedStyle(root).getPropertyValue("--red").trim() || "red",
    blue: getComputedStyle(root).getPropertyValue("--blue").trim() || "blue"
  };

  const normalizePoint = (raw, prefix, index) => {
    const lon = Number(Array.isArray(raw) ? raw[0] : (raw.lon ?? raw.lng));
    const lat = Number(Array.isArray(raw) ? raw[1] : raw.lat);
    const rawCount = Array.isArray(raw) ? (raw[2] ?? 1) : (raw.count ?? raw.order_count ?? raw.pickup_count ?? raw.dropoff_count ?? 1);
    const count = Number(rawCount);
    if (!Number.isFinite(lon) || !Number.isFinite(lat) || Math.abs(lon) > 180 || Math.abs(lat) > 90 || !Number.isFinite(count) || count < 0) throw new Error(`${prefix} 第 ${index + 1} 个点字段无效`);
    return { id: String(Array.isArray(raw) ? `${prefix}_${index + 1}` : (raw.point_id ?? raw.id ?? `${prefix}_${index + 1}`)), lon, lat, count };
  };
  const endpointCoordinate = (raw, prefix, index) => {
    const point = normalizePoint(raw, prefix, index);
    return { lon: point.lon, lat: point.lat };
  };
  function normalizePackage(pkg) {
    if (!pkg || pkg.package_type !== "map_order_demand_package" || Number(pkg.schema_version) !== 1) throw new Error("数据包格式不匹配：需要 map_order_demand_package v1 JSON");
    const metadata = pkg.metadata || {};
    let pickups = Array.isArray(pkg.pickup_points) ? pkg.pickup_points.map((point, index) => normalizePoint(point, "pickup", index)) : [];
    let dropoffs = Array.isArray(pkg.dropoff_points) ? pkg.dropoff_points.map((point, index) => normalizePoint(point, "dropoff", index)) : [];
    const demandNodeMap = new Map();
    const demandNodeArray = Array.isArray(pkg.demand_nodes) ? pkg.demand_nodes : [];
    demandNodeArray.forEach((node, index) => {
      const point = normalizePoint(node, "demand_node", index);
      const id = String(Array.isArray(node) ? index : (node.node_id ?? node.id ?? index));
      demandNodeMap.set(id, point);
    });
    const resolveEndpoint = (value, fallbackId, index) => {
      if (typeof value === "number" && demandNodeArray[value]) return normalizePoint(demandNodeArray[value], "demand_node", value);
      const id = value == null ? fallbackId : String(value);
      if (demandNodeMap.has(id)) return demandNodeMap.get(id);
      if (value && typeof value === "object") return endpointCoordinate(value, "demand", index);
      throw new Error(`需求端点 ${id} 未在 demand_nodes 中定义`);
    };
    let pairs = [];
    if (Array.isArray(pkg.demand_pairs) && pkg.demand_pairs.length) {
      pairs = pkg.demand_pairs.map((pair, index) => {
        let origin, destination, count;
        if (Array.isArray(pair)) {
          origin = resolveEndpoint(pair[0], null, index); destination = resolveEndpoint(pair[1], null, index); count = Number(pair[2] ?? 1);
        } else {
          origin = pair.origin || pair.pickup || resolveEndpoint(pair.origin_node_id, null, index);
          destination = pair.destination || pair.dropoff || resolveEndpoint(pair.destination_node_id, null, index);
          if (origin && !Number.isFinite(Number(origin.lon))) origin = endpointCoordinate(origin, "origin", index);
          if (destination && !Number.isFinite(Number(destination.lon))) destination = endpointCoordinate(destination, "destination", index);
          count = Number(pair.order_count ?? pair.count ?? 1);
        }
        if (!origin || !destination || !Number.isFinite(count) || count <= 0) throw new Error(`需求 OD 第 ${index + 1} 行无效`);
        return { origin: [origin.lon, origin.lat], destination: [destination.lon, destination.lat], count };
      });
    } else if (Array.isArray(pkg.orders) && pkg.orders.length) {
      pairs = pkg.orders.map((order, index) => {
        if (!order.pickup || !order.dropoff) throw new Error(`订单 ${index + 1} 缺少 pickup/dropoff`);
        const pickup = endpointCoordinate(order.pickup, "pickup", index), dropoff = endpointCoordinate(order.dropoff, "dropoff", index);
        const count = Number(order.order_count ?? order.count ?? 1);
        if (!Number.isFinite(count) || count <= 0) throw new Error(`订单 ${index + 1} 数量无效`);
        return { origin: [pickup.lon, pickup.lat], destination: [dropoff.lon, dropoff.lat], count };
      });
    } else throw new Error("数据包必须包含 demand_pairs 或 orders");
    if (!pickups.length) {
      const accumulated = new Map();
      for (const pair of pairs) { const key = coordinateKey(pair.origin); accumulated.set(key, (accumulated.get(key) || 0) + pair.count); }
      pickups = [...accumulated.entries()].map(([key, count], index) => { const [lon, lat] = key.split(",").map(n => Number(n) / 1e6); return { id: `pickup_${index + 1}`, lon, lat, count }; });
    }
    if (!dropoffs.length) {
      const accumulated = new Map();
      for (const pair of pairs) { const key = coordinateKey(pair.destination); accumulated.set(key, (accumulated.get(key) || 0) + pair.count); }
      dropoffs = [...accumulated.entries()].map(([key, count], index) => { const [lon, lat] = key.split(",").map(n => Number(n) / 1e6); return { id: `dropoff_${index + 1}`, lon, lat, count }; });
    }
    if (!pickups.length || !dropoffs.length) throw new Error("数据包没有有效的上车点或下车点");
    return { metadata, pickups, dropoffs, pairs, totalCount: pairs.reduce((sum, pair) => sum + pair.count, 0) };
  }

  const pointRadius = (count, maximum) => 1.3 + 1.35 * Math.sqrt(Math.log1p(count) / Math.log1p(maximum || 1));
  function renderImportedPoints() {
    if (!activePackage) return;
    const pickupMax = d3.max(activePackage.pickups, item => item.count) || 1;
    const dropoffMax = d3.max(activePackage.dropoffs, item => item.count) || 1;
    groups.get("pickups").selectAll("circle").data(activePackage.pickups, item => item.id).join("circle")
      .attr("cx", item => project([item.lon, item.lat])[0]).attr("cy", item => project([item.lon, item.lat])[1])
      .attr("r", item => pointRadius(item.count, pickupMax)).attr("class", "pickup-point")
      .attr("data-tooltip", item => `数据包上车点聚合 ${item.count.toLocaleString()} 次`);
    groups.get("dropoffs").selectAll("path").data(activePackage.dropoffs, item => item.id).join("path")
      .attr("d", item => d3.symbol().type(d3.symbolDiamond).size(5 + 9 * Math.sqrt(Math.log1p(item.count) / Math.log1p(dropoffMax)))())
      .attr("transform", item => `translate(${project([item.lon, item.lat])[0]},${project([item.lon, item.lat])[1]})`)
      .attr("class", "dropoff-point").attr("data-tooltip", item => `数据包下车点聚合 ${item.count.toLocaleString()} 次`);
  }
  function renderCandidates() {
    const topN = Math.max(1, Math.min(5000, Math.floor(Number(topNInput.value) || 30)));
    const threshold = Math.max(0, Number(thresholdInput.value) || 0);
    const ordered = [...activePackage.pickups].sort((a, b) => b.count - a.count || a.id.localeCompare(b.id));
    const selected = new Map();
    ordered.slice(0, topN).forEach(item => selected.set(item.id, item));
    if (threshold > 0) ordered.filter(item => item.count >= threshold).forEach(item => selected.set(item.id, item));
    selectedCandidates = [...selected.values()].sort((a, b) => b.count - a.count || a.id.localeCompare(b.id)).map((item, index) => ({ ...item, rank: index + 1, snap: nearestNode([item.lon, item.lat], maxSnapMeters) }));
    groups.get("candidates").selectAll("circle").data(selectedCandidates, item => item.id).join("circle")
      .attr("cx", item => project([item.lon, item.lat])[0]).attr("cy", item => project([item.lon, item.lat])[1])
      .attr("r", 8).attr("class", "candidate-point")
      .attr("data-tooltip", item => `筛选候选点 #${item.rank} · 历史上车计数 ${item.count.toLocaleString()}${item.snap ? ` · 最近路网节点 ${item.snap.distance.toFixed(0)} m` : " · 300 m内未匹配机动车道路"}`);
    groups.get("candidates").selectAll("text").data(selectedCandidates, item => item.id).join("text")
      .attr("x", item => project([item.lon, item.lat])[0]).attr("y", item => project([item.lon, item.lat])[1])
      .attr("class", "candidate-rank").text(item => item.rank);
    groups.get("candidates").raise();
    root.querySelector("#candidate-top-label").textContent = `Top ${topN}`;
    root.querySelector("#candidate-threshold-label").textContent = threshold > 0 ? `阈值 ≥ ${threshold.toLocaleString()}` : "阈值关闭";
    candidateSummary.textContent = `筛选出 ${selectedCandidates.length.toLocaleString()} 个候选点（Top ${topN}${threshold > 0 ? ` ∪ 上车计数 ≥ ${threshold.toLocaleString()}` : ""}）；其中 ${selectedCandidates.filter(item => !item.snap).length} 个点距可路由道路节点超过 ${maxSnapMeters} m。`;
  }

  async function renderCandidateLinks() {
    const useCounts = new Map();
    const snapped = selectedCandidates.filter(item => item.snap);
    const bySource = new Map();
    for (const point of snapped) {
      if (!bySource.has(point.snap.node.key)) bySource.set(point.snap.node.key, []);
      bySource.get(point.snap.node.key).push(point);
    }
    candidatePairCount = selectedCandidates.length * Math.max(0, selectedCandidates.length - 1);
    candidatePairReachable = 0;
    let processedSources = 0;
    for (const [sourceKey, origins] of bySource) {
      const targets = [...new Set(snapped.filter(item => item.snap.node.key !== sourceKey).map(item => item.snap.node.key))];
      const result = dijkstra(sourceKey, targets);
      for (const origin of origins) {
        for (const destination of selectedCandidates) {
          if (origin.id === destination.id || !destination.snap) continue;
          if (origin.snap.node.key === destination.snap.node.key) { candidatePairReachable++; continue; }
          const path = recoverPath(result, destination.snap.node.key);
          if (!path) continue;
          candidatePairReachable++;
          for (const edge of path) useCounts.set(edge.segmentId, (useCounts.get(edge.segmentId) || 0) + 1);
        }
      }
      if (++processedSources % 8 === 0) {
        packageStatus.textContent = `已筛选 ${selectedCandidates.length} 个候选点；Dijkstra 连接计算 ${processedSources}/${bySource.size} 个起点…`;
        await yieldToUI();
      }
    }
    maxLinkUse = Math.max(1, ...useCounts.values());
    const linkRows = [...useCounts.entries()].map(([segmentId, count]) => ({ segment: physicalSegments[segmentId], count }));
    groups.get("links").selectAll("path.link-segment").data(linkRows, item => item.segment.id).join("path")
      .attr("d", item => geoPath({ type: "LineString", coordinates: [item.segment.a, item.segment.b] }))
      .attr("class", "link-segment")
      .attr("stroke-width", item => 0.65 + 1.05 * Math.log1p(item.count) / Math.log1p(maxLinkUse))
      .attr("data-tooltip", item => `Dijkstra候选连接路段 · OSM Way ${item.segment.wayId} · 被 ${item.count} 条有向候选点最短路共用`);
    const accessRows = snapped.filter(item => item.snap.distance > 3).map(item => ({ item, point: [item.lon, item.lat], node: item.snap.node.coordinate }));
    groups.get("links").selectAll("path.link-access").data(accessRows, row => row.item.id).join("path")
      .attr("d", row => geoPath({ type: "LineString", coordinates: [row.point, row.node] }))
      .attr("class", "link-access").attr("data-tooltip", row => `候选点 #${row.item.rank} 到最近可路由节点接入连接 · ${row.item.snap.distance.toFixed(0)} m`);
    const misses = selectedCandidates.length - snapped.length;
    const summary = `候选点有序点对 ${candidatePairCount.toLocaleString()} 个，Dijkstra 可达 ${candidatePairReachable.toLocaleString()} 个；${linkRows.length.toLocaleString()} 条底层道路片段构成最短路 Link 并按共用次数合并显示；${misses} 个候选点没有 300 m 内接入节点。`;
    root.querySelector("#link-summary").textContent = summary;
  }

  async function calculateDemandLoads() {
    loadBySegment = new Map(); unroutedPairCount = 0; routedDemand = 0; sameNodePairCount = 0; sameNodeDemand = 0; totalDemand = activePackage.totalCount;
    const grouped = new Map();
    for (const pair of activePackage.pairs) {
      const origin = nearestNode(pair.origin, maxSnapMeters), destination = nearestNode(pair.destination, maxSnapMeters);
      if (!origin || !destination) { unroutedPairCount++; continue; }
      if (origin.node.key === destination.node.key) { sameNodePairCount++; sameNodeDemand += pair.count; continue; }
      if (!grouped.has(origin.node.key)) grouped.set(origin.node.key, []);
      grouped.get(origin.node.key).push({ destination: destination.node.key, count: pair.count });
    }
    let groupIndex = 0;
    for (const [sourceKey, pairs] of grouped) {
      const targets = [...new Set(pairs.map(pair => pair.destination))];
      const result = dijkstra(sourceKey, targets);
      for (const pair of pairs) {
        const path = recoverPath(result, pair.destination);
        if (!path) { unroutedPairCount++; continue; }
        routedDemand += pair.count;
        for (const edge of path) loadBySegment.set(edge.segmentId, (loadBySegment.get(edge.segmentId) || 0) + pair.count);
      }
      if (++groupIndex % 8 === 0) {
        packageStatus.textContent = `需求路由分配 ${groupIndex}/${grouped.size} 个起点；正在累计各道路 OD 流量…`;
        await yieldToUI();
      }
    }
    maxRoadLoad = Math.max(1, ...loadBySegment.values());
    peakSnapFailures = unroutedPairCount;
    dropoffSnapFailures = activePackage.dropoffs.filter(item => !nearestNode([item.lon, item.lat], maxSnapMeters)).length;
    renderTraffic();
  }

  const phaseAt = (time, signalIndex) => {
    const position = ((time + signalIndex * 11) % cycle + cycle) % cycle;
    if (position < greenSeconds) return { name: "绿", color: "green", remaining: 0 };
    if (position < greenSeconds + yellowSeconds) return { name: "黄", color: "yellow", remaining: greenSeconds + yellowSeconds - position + redSeconds };
    return { name: "红", color: "red", remaining: cycle - position };
  };
  const speedForClass = roadClass => ({ motorway: 70, trunk: 60, primary: 50, secondary: 40, tertiary: 35, residential: 30, service: 20 }[roadClass] || 25);
  function renderTraffic() {
    if (!activePackage) return;
    const signalStates = signalFeatures.map((feature, index) => ({ ...phaseAt(sim.time, index), name: feature.properties.id || `信号灯${index + 1}` }));
    groups.get("signals").selectAll("circle")
      .attr("class", (_, index) => `signal-point signal-${signalStates[index].color}`)
      .attr("r", 3.1)
      .attr("data-tooltip", (_, index) => `OSM 信号灯位置 · 当前仿真相位 ${signalStates[index].name}灯；非实时采集`);
    const backgroundFactor = 0.5 + 0.5 * Math.sin((sim.time / 180) * Math.PI * 2 - Math.PI / 2);
    const rows = physicalSegments.filter(segment => (loadBySegment.get(segment.id) || 0) > 0).map(segment => {
      const load = loadBySegment.get(segment.id) || 0;
      const loadRatio = Math.log1p(load) / Math.log1p(maxRoadLoad);
      const signalIndex = segment.signalIndexes[0] ?? -1;
      const signal = signalIndex >= 0 ? signalStates[signalIndex] : null;
      const signalRatio = signal ? signal.remaining / redSeconds : 0;
      const dynamicDemand = loadRatio * (0.65 + 0.35 * backgroundFactor);
      const queue = Math.min(30, 18 * dynamicDemand + (signal && signal.color === "red" ? 3 : 0));
      const freeFlow = segment.length / (speedForClass(segment.roadClass) / 3.6);
      const signalWait = signal ? signal.remaining : 0;
      const passTime = freeFlow + queue / 0.45 + signalWait;
      const score = Math.max(0, Math.min(1, 0.78 * dynamicDemand + 0.22 * signalRatio));
      return { segment, load, loadRatio, signal, queue, freeFlow, signalWait, passTime, score };
    });
    trafficMetrics = rows;
    const colorAt = score => d3.interpolateRgb(routePalette.green, routePalette.red)(score);
    groups.get("congestion").selectAll("path.congestion-segment").data(rows, item => item.segment.id).join("path")
      .attr("d", item => geoPath({ type: "LineString", coordinates: [item.segment.a, item.segment.b] }))
      .attr("class", "congestion-segment")
      .attr("stroke", item => colorAt(item.score))
      .attr("stroke-width", item => 1.2 + 2 * item.score)
      .attr("data-tooltip", item => `订单包加权经过 ${item.load.toLocaleString()} 次 · 拥堵指标 ${(item.score * 100).toFixed(0)}% · ${item.signal ? `${item.signal.name}灯` : "无邻近信号"} · 估算通行 ${item.passTime.toFixed(1)} 秒`);
    const affected = rows.filter(item => item.load > 0).sort((a, b) => b.score - a.score || b.load - a.load);
    const meanPass = affected.length ? affected.reduce((sum, item) => sum + item.passTime, 0) / affected.length : 0;
    const green = signalStates.filter(item => item.color === "green").length;
    const yellow = signalStates.filter(item => item.color === "yellow").length;
    const red = signalStates.length - green - yellow;
    root.querySelector("#traffic-time-label").textContent = `${String(Math.floor(sim.time / 60)).padStart(2, "0")}:${String(sim.time % 60).padStart(2, "0")}`;
    root.querySelector("#traffic-stats").textContent = `${routedDemand.toLocaleString()} / ${totalDemand.toLocaleString()} 次需求映射到有长度的道路路径 · ${affected.length.toLocaleString()} 条路段承载需求 · 同一接入节点 OD ${sameNodeDemand.toLocaleString()} 次未计入道路负载 · 当前信号 ${green} 绿 / ${yellow} 黄 / ${red} 红 · 受影响路段平均估算 ${meanPass.toFixed(1)} 秒 · 背景交通系数 ${(backgroundFactor * 100).toFixed(0)}%`;
    const tbody = root.querySelector("#traffic-road-rows");
    tbody.replaceChildren();
    for (const item of affected.slice(0, 12)) {
      const row = document.createElement("tr");
      [String(item.segment.wayId), String(item.segment.segmentIndex + 1), item.load.toLocaleString(), item.signal ? item.signal.name : "无", `${item.queue.toFixed(1)} 辆`, `${item.passTime.toFixed(1)} 秒`].forEach(value => {
        const cell = document.createElement("td"); cell.textContent = value; row.append(cell);
      });
      tbody.append(row);
    }
    root.querySelector("#traffic-road-rows").dataset.rowCount = String(Math.min(12, affected.length));
    calculationSummary.textContent = `当前已用 ${activePackage.metadata.name || activePackage.metadata.source || "导入数据包"}：${activePackage.pickups.length.toLocaleString()} 个上车网格、${activePackage.dropoffs.length.toLocaleString()} 个下车网格、${activePackage.pairs.length.toLocaleString()} 个 OD 需求对（加权 ${totalDemand.toLocaleString()} 次）。Dijkstra 按最短距离把 OD 对分配到单行约束下的基准道路；同一接入节点的 ${sameNodePairCount.toLocaleString()} 个 OD 对（${sameNodeDemand.toLocaleString()} 次）没有可计长度的路段，未计入路段负载。其余需求按次数累加到经过的路段；仿真时钟改变模拟信号相位和背景交通系数，进而更新排队估计、通行时间与绿—黄—红颜色。${unroutedPairCount.toLocaleString()} 个 OD 对未能接入当前机动车图。`;
  }

  async function recalculateCandidateSelection() {
    if (!activePackage) return;
    packageStatus.textContent = "按上车热度筛选候选点…";
    renderCandidates();
    await renderCandidateLinks();
    updateCounts();
    packageStatus.textContent = `已导入 ${activePackage.metadata.name || "数据包"}；候选点和 Dijkstra Link 已计算。`;
  }
  function updateCounts() {
    mapCounts.textContent = `原始道路 ${baseCounts.rawHighwayWays.toLocaleString()} 条 OSM highway Way · 可路由候选道路 ${baseCounts.routingCandidateRoads.toLocaleString()} 条 · OSM 信号灯 ${signalFeatures.length.toLocaleString()} 处 · 数据包上车点 ${activePackage.pickups.length.toLocaleString()} 个、下车点 ${activePackage.dropoffs.length.toLocaleString()} 个 · 候选点 ${selectedCandidates.length.toLocaleString()} 个 · 可达 Dijkstra 点对 ${candidatePairReachable.toLocaleString()} / ${candidatePairCount.toLocaleString()} · OD需求 ${totalDemand.toLocaleString()} 次`;
    packageSummary.textContent = `${activePackage.metadata.name || activePackage.metadata.source || "导入数据包"} · 来源类型 ${activePackage.metadata.source_type || "未注明"} · 加权 OD ${totalDemand.toLocaleString()} 次 · 数据包不包含原始轨迹/个人标识`;
  }

  fileInput.addEventListener("change", async event => {
    const file = event.currentTarget.files && event.currentTarget.files[0];
    if (!file) return;
    if (file.size > 25 * 1024 * 1024) { packageStatus.textContent = "数据包超过 25 MB 上限。"; return; }
    packageStatus.textContent = `正在解析 ${file.name}…`;
    try {
      const parsed = normalizePackage(JSON.parse(await file.text()));
      activePackage = parsed;
      for (const input of layerInputs) if (initiallyUnavailable.has(input.dataset.layer)) input.disabled = false;
      renderImportedPoints();
      packageStatus.textContent = "数据包已解析；正在将 OD 需求分配到道路…";
      await calculateDemandLoads();
      await recalculateCandidateSelection();
      updateCounts();
      fileInput.dataset.loadedName = file.name;
    } catch (error) {
      packageStatus.textContent = `导入失败：${error.message || String(error)}`;
    }
  });
  for (const input of [topNInput, thresholdInput]) input.addEventListener("change", () => { recalculateCandidateSelection(); });

  const updateTime = value => {
    sim.time = Math.max(0, Math.min(180, Math.round(Number(value) / 5) * 5));
    timeSlider.value = sim.time;
    if (activePackage) renderTraffic();
  };
  timeSlider.addEventListener("input", event => updateTime(event.currentTarget.value));
  root.querySelector("#traffic-step").addEventListener("click", () => updateTime(sim.time + 5));
  root.querySelector("#traffic-reset").addEventListener("click", () => {
    if (sim.timer) clearInterval(sim.timer); sim.timer = null;
    root.querySelector("#traffic-play").textContent = "播放"; updateTime(0);
  });
  root.querySelector("#traffic-play").addEventListener("click", event => {
    if (sim.timer) { clearInterval(sim.timer); sim.timer = null; event.currentTarget.textContent = "播放"; return; }
    event.currentTarget.textContent = "暂停";
    sim.timer = setInterval(() => {
      if (sim.time >= 180) { clearInterval(sim.timer); sim.timer = null; root.querySelector("#traffic-play").textContent = "播放"; return; }
      updateTime(sim.time + 5);
    }, 500);
  });
})();
