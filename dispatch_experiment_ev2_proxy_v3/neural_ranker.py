"""Small, auditable PyTorch pairwise ranker for dispatch candidates.

The ranker imitates pairwise preferences induced by the deterministic D1
optimizer. It only prunes the per-vehicle candidate list; hard constraints,
maximum-matching protection, and final assignment remain program controlled.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .models import Candidate


RANKER_SCHEMA_VERSION = "dispatch-candidate-ranker-v1"
RANKER_FEATURE_NAMES = (
    "local_order_density",
    "local_idle_vehicle_density",
    "demand_supply_ratio",
    "commitment_slack",
    "candidate_eta",
    "time_context",
    "vehicle_state_code",
    "predicted_wait_ratio",
    "pickup_distance_10km",
    "ride_distance_10km",
    "ride_duration_ratio",
    "next_order_handoff",
)
HIDDEN_SIZES = (16, 8)


def _read(candidate: Any, key: str, default: Any = None) -> Any:
    if isinstance(candidate, Mapping):
        return candidate.get(key, default)
    return getattr(candidate, key, default)


def _leg_value(candidate: Any, leg_name: str, field: str) -> float:
    leg = _read(candidate, leg_name, {})
    if isinstance(leg, Mapping):
        value = leg.get(field, 0.0)
    else:
        value = getattr(leg, field, 0.0)
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def candidate_feature_row(
    candidate: Any, *, max_wait_sec: float, horizon_sec: float
) -> List[float]:
    """Create the frozen v1 feature vector from the current candidate snapshot."""
    context = _read(candidate, "feature_vector", {}) or {}
    if not isinstance(context, Mapping):
        context = {}
    pickup_leg = _leg_value(candidate, "pickup_leg", "distance_m")
    ride_leg = _leg_value(candidate, "ride_leg", "distance_m")
    ride_duration = _leg_value(candidate, "ride_leg", "duration_sec")
    wait = _read(candidate, "predicted_wait_sec", 0.0)
    try:
        wait = float(wait)
    except (TypeError, ValueError):
        wait = 0.0
    if not math.isfinite(wait):
        wait = max_wait_sec
    candidate_type = str(_read(candidate, "candidate_type", "immediate_pickup"))
    base = [
        float(context.get("local_order_density", 0.0)) / 10.0,
        float(context.get("local_idle_vehicle_density", 0.0)),
        float(context.get("demand_supply_ratio", 0.0)) / 10.0,
        float(context.get("commitment_slack", 0.0)),
        float(context.get("candidate_eta", 0.0)) / 2.0,
        float(context.get("time_context", 0.0)),
        float(context.get("vehicle_state_code", 0.0)) / 4.0,
        max(0.0, wait) / max(1.0, max_wait_sec),
        max(0.0, pickup_leg) / 10_000.0,
        max(0.0, ride_leg) / 10_000.0,
        max(0.0, ride_duration) / max(1.0, horizon_sec),
        1.0 if candidate_type == "next_order_handoff" else 0.0,
    ]
    row = [0.0 if not math.isfinite(value) else float(value) for value in base]
    if len(row) != len(RANKER_FEATURE_NAMES):
        raise ValueError("ranker_feature_count_mismatch")
    return row


def _torch_modules():
    try:
        import torch
        from torch import nn
    except ImportError as error:  # pragma: no cover - environment dependent
        raise RuntimeError("PyTorch is required for the dispatch neural ranker") from error
    return torch, nn


def _network_class(nn):
    class CandidateRankerNetwork(nn.Module):
        def __init__(self, input_size: int):
            super().__init__()
            self.layers = nn.Sequential(
                nn.Linear(input_size, HIDDEN_SIZES[0]),
                nn.ReLU(),
                nn.Linear(HIDDEN_SIZES[0], HIDDEN_SIZES[1]),
                nn.ReLU(),
                nn.Linear(HIDDEN_SIZES[1], 1),
            )

        def forward(self, inputs):
            return self.layers(inputs).squeeze(-1)

    return CandidateRankerNetwork


class NeuralCandidateRanker:
    """Load a JSON checkpoint and score candidates deterministically on CPU."""

    def __init__(self, checkpoint_path: Path | str):
        self.checkpoint_path = Path(checkpoint_path).resolve()
        self.checkpoint_sha256 = hashlib.sha256(self.checkpoint_path.read_bytes()).hexdigest()
        self.payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
        if self.payload.get("schema_version") != RANKER_SCHEMA_VERSION:
            raise ValueError("ranker_checkpoint_schema_mismatch")
        if self.payload.get("feature_names") != list(RANKER_FEATURE_NAMES):
            raise ValueError("ranker_checkpoint_feature_mismatch")
        torch, nn = _torch_modules()
        torch.set_num_threads(1)
        self.torch = torch
        self.mean = torch.tensor(self.payload["normalization"]["mean"], dtype=torch.float32)
        self.scale = torch.tensor(self.payload["normalization"]["scale"], dtype=torch.float32)
        model_factory = _network_class(nn)
        self.model = model_factory(len(RANKER_FEATURE_NAMES))
        state = {
            name: torch.tensor(values, dtype=torch.float32)
            for name, values in self.payload["state_dict"].items()
        }
        self.model.load_state_dict(state, strict=True)
        self.model.eval()

    @property
    def model_id(self) -> str:
        return str(self.payload.get("model_id", self.checkpoint_sha256[:16]))

    def score_rows(self, rows: Sequence[Sequence[float]]) -> List[float]:
        if not rows:
            return []
        values = self.torch.tensor(rows, dtype=self.torch.float32)
        normalized = (values - self.mean) / self.scale
        with self.torch.inference_mode():
            scores = self.model(normalized).cpu().tolist()
        if any(not math.isfinite(float(value)) for value in scores):
            raise ValueError("ranker_non_finite_score")
        return [float(value) for value in scores]

    def score_candidates(
        self,
        candidates: Sequence[Candidate],
        *,
        max_wait_sec: float,
        horizon_sec: float,
    ) -> List[float]:
        rows = [
            candidate_feature_row(
                item, max_wait_sec=max_wait_sec, horizon_sec=horizon_sec
            )
            for item in candidates
        ]
        return self.score_rows(rows)


def _stable_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_json_header_value(path: Path, key: str) -> Any:
    """Read one small top-level value before the large rounds array."""
    with path.open("r", encoding="utf-8") as stream:
        prefix = stream.read(2 * 1024 * 1024)
    marker = '"' + key + '"'
    position = prefix.find(marker)
    if position < 0:
        raise ValueError("training_result_header_missing_" + key)
    colon = prefix.find(":", position + len(marker))
    if colon < 0:
        raise ValueError("training_result_header_invalid_" + key)
    start = colon + 1
    while start < len(prefix) and prefix[start].isspace():
        start += 1
    try:
        value, _ = json.JSONDecoder().raw_decode(prefix, start)
    except json.JSONDecodeError as error:
        raise ValueError("training_result_header_invalid_" + key) from error
    return value


def _iter_json_array(path: Path, key: str):
    """Yield items from a top-level JSON array without loading the full file."""
    with path.open("r", encoding="utf-8") as stream:
        prefix = stream.read(2 * 1024 * 1024)
        marker = '"' + key + '"'
        position = prefix.find(marker)
        if position < 0:
            raise ValueError("training_result_array_missing_" + key)
        colon = prefix.find(":", position + len(marker))
        array_start = prefix.find("[", colon + 1) if colon >= 0 else -1
        if array_start < 0:
            raise ValueError("training_result_array_invalid_" + key)
        stream.seek(0)
        stream.read(array_start + 1)
        decoder = json.JSONDecoder()
        buffer = ""
        while True:
            if not buffer:
                buffer = stream.read(1024 * 1024)
                if not buffer:
                    raise ValueError("training_result_array_unterminated_" + key)
            start = 0
            while start < len(buffer) and (buffer[start].isspace() or buffer[start] == ","):
                start += 1
            if start == len(buffer):
                buffer = ""
                continue
            if buffer[start] == "]":
                return
            try:
                item, end = decoder.raw_decode(buffer, start)
            except json.JSONDecodeError:
                more = stream.read(1024 * 1024)
                if not more:
                    raise ValueError("training_result_array_item_invalid_" + key)
                buffer += more
                continue
            yield item
            buffer = buffer[end:]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def collect_pairwise_training_data(
    result_paths: Sequence[Path | str],
    *,
    max_wait_sec: Optional[float] = None,
    max_negatives_per_positive: int = 64,
) -> Tuple[List[Tuple[List[float], List[float]]], Dict[str, Any]]:
    """Build D1-imitation pairs from full-record D1 results.

    A negative must conflict with a selected edge through the same vehicle or
    order. Unrelated edges are not treated as inferior examples.
    """
    pairs: List[Tuple[List[float], List[float]]] = []
    run_metadata = []
    all_order_ids = set()
    for raw_path in result_paths:
        path = Path(raw_path).resolve()
        method_name = _read_json_header_value(path, "method_name")
        recording_mode = _read_json_header_value(path, "recording_mode")
        if method_name != "D1_optimizer":
            raise ValueError("ranker_training_source_must_be_D1_optimizer")
        if recording_mode not in {"full_round_records", "compact_training_label_rounds"}:
            raise ValueError("ranker_training_source_requires_full_or_compact_training_records")
        config = _read_json_header_value(path, "config")
        wait_limit = float(max_wait_sec or config.get("max_pickup_wait_sec", 300.0))
        horizon = float(config.get("horizon_sec", 3600.0))
        window_order_ids = set()
        round_count = 0
        feasible_count = 0
        selected_count = 0
        pair_count = 0
        compact = recording_mode == "compact_training_label_rounds"
        records = _iter_json_array(path, "training_rounds" if compact else "rounds")
        for record in records:
            if compact:
                candidates = record.get("candidates", [])
                selected_ids = set(record.get("selected_candidate_ids", []))
            else:
                dispatch_input = record.get("input", {})
                dispatch_output = record.get("output", {})
                candidates = dispatch_input.get("candidates", [])
                selected_ids = set(dispatch_output.get("selected_candidate_ids", []))
            feasible = [candidate for candidate in candidates if candidate.get("hard_constraint_ok")]
            if not feasible or not selected_ids:
                round_count += 1
                continue
            feasible_count += len(feasible)
            selected = [candidate for candidate in feasible if candidate.get("candidate_id") in selected_ids]
            selected_count += len(selected)
            window_order_ids.update(str(item.get("order_id")) for item in feasible)
            by_id = {str(item.get("candidate_id")): item for item in feasible}
            selected_ids.intersection_update(by_id)
            negatives = [item for item in feasible if item.get("candidate_id") not in selected_ids]
            for positive in selected:
                positive_vehicle = positive.get("vehicle_id")
                positive_order = positive.get("order_id")
                conflicts = [
                    negative for negative in negatives
                    if negative.get("vehicle_id") == positive_vehicle
                    or negative.get("order_id") == positive_order
                ]
                conflicts.sort(
                    key=lambda item: _stable_key(
                        str(record.get("dispatch_round_id", ""))
                        + "|" + str(positive.get("candidate_id", ""))
                        + "|" + str(item.get("candidate_id", ""))
                    )
                )
                for negative in conflicts[:max_negatives_per_positive]:
                    pos_row = candidate_feature_row(
                        positive, max_wait_sec=wait_limit, horizon_sec=horizon
                    )
                    neg_row = candidate_feature_row(
                        negative, max_wait_sec=wait_limit, horizon_sec=horizon
                    )
                    pairs.append((pos_row, neg_row))
                    pair_count += 1
            round_count += 1
        overlap = all_order_ids.intersection(window_order_ids)
        if overlap:
            raise ValueError("ranker_training_windows_share_order_ids")
        all_order_ids.update(window_order_ids)
        run_metadata.append({
            "path": str(path),
            "sha256": _sha256_file(path),
            "scenario_id": _read_json_header_value(path, "scenario_id"),
            "case_id": _read_json_header_value(path, "case_id"),
            "fleet_id": _read_json_header_value(path, "fleet_id"),
            "scenario_fingerprint_sha256": _read_json_header_value(path, "scenario_fingerprint_sha256"),
            "input_hashes": _read_json_header_value(path, "input_hashes"),
            "recording_mode": recording_mode,
            "round_count": round_count,
            "hard_feasible_candidate_count": feasible_count,
            "selected_candidate_count": selected_count,
            "pairwise_sample_count": pair_count,
            "order_id_count": len(window_order_ids),
        })
    if not pairs:
        raise ValueError("ranker_training_pairs_empty")
    summary = {
        "training_label": "D1_optimizer_pairwise_imitation",
        "training_runs": run_metadata,
        "training_window_count": len(run_metadata),
        "training_order_id_count": len(all_order_ids),
        "pairwise_sample_count": len(pairs),
        "max_negatives_per_positive": max_negatives_per_positive,
        "feature_names": list(RANKER_FEATURE_NAMES),
    }
    return pairs, summary


def train_pairwise_ranker(
    pairs: Sequence[Tuple[Sequence[float], Sequence[float]]],
    *,
    seed: int = 17,
    epochs: int = 120,
    learning_rate: float = 0.01,
    weight_decay: float = 1e-4,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if not pairs:
        raise ValueError("ranker_training_pairs_empty")
    torch, nn = _torch_modules()
    torch.manual_seed(seed)
    torch.set_num_threads(1)
    torch.use_deterministic_algorithms(True)
    left = torch.tensor([pair[0] for pair in pairs], dtype=torch.float32)
    right = torch.tensor([pair[1] for pair in pairs], dtype=torch.float32)
    combined = torch.cat((left, right), dim=0)
    mean = combined.mean(dim=0)
    scale = combined.std(dim=0, unbiased=False).clamp_min(1e-6)
    left = (left - mean) / scale
    right = (right - mean) / scale

    model_factory = _network_class(nn)
    model = model_factory(len(RANKER_FEATURE_NAMES))
    optimizer = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    for _ in range(epochs):
        optimizer.zero_grad(set_to_none=True)
        margins = model(left) - model(right)
        loss = torch.nn.functional.softplus(-margins).mean()
        loss.backward()
        optimizer.step()
    with torch.inference_mode():
        margins = model(left) - model(right)
        train_accuracy = float((margins > 0).float().mean().item())
        final_loss = float(torch.nn.functional.softplus(-margins).mean().item())
    payload = {
        "schema_version": RANKER_SCHEMA_VERSION,
        "model_id": "mlp_pairwise_d1_imitation_v1_seed_" + str(seed),
        "architecture": {
            "kind": "mlp_pairwise_ranker",
            "input_size": len(RANKER_FEATURE_NAMES),
            "hidden_sizes": list(HIDDEN_SIZES),
            "activation": "ReLU",
            "output": "scalar_candidate_score",
        },
        "feature_names": list(RANKER_FEATURE_NAMES),
        "normalization": {
            "mean": [float(value) for value in mean.tolist()],
            "scale": [float(value) for value in scale.tolist()],
        },
        "state_dict": {
            name: tensor.detach().cpu().tolist()
            for name, tensor in model.state_dict().items()
        },
        "training": {
            "label_source": "D1 deterministic global optimizer selected candidate edges",
            "loss": "pairwise_softplus",
            "seed": seed,
            "epochs": epochs,
            "learning_rate": learning_rate,
            "weight_decay": weight_decay,
            "pairwise_sample_count": len(pairs),
            "in_sample_pair_accuracy": train_accuracy,
            "final_pairwise_loss": final_loss,
            "torch_version": torch.__version__,
            "device": "cpu",
        },
    }
    report = {
        "pairwise_sample_count": len(pairs),
        "in_sample_pair_accuracy": train_accuracy,
        "final_pairwise_loss": final_loss,
        "seed": seed,
        "epochs": epochs,
        "torch_version": torch.__version__,
    }
    return payload, report


def save_ranker_checkpoint(path: Path | str, payload: Dict[str, Any]) -> str:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
    target.write_bytes(data)
    return hashlib.sha256(data).hexdigest()
