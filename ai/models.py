"""PyTorch models used by the demo training scripts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

import torch
from torch import nn


@dataclass
class ModelSpec:
    task: str
    input_dim: int
    output_dim: int
    hidden_dim: int = 96
    continuous: bool = False


class MLPPolicy(nn.Module):
    def __init__(self, input_dim: int, output_dim: int, hidden_dim: int = 96, continuous: bool = False):
        super().__init__()
        self.continuous = continuous
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, output_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.net(x)
        if self.continuous:
            return torch.tanh(out)
        return out


def save_checkpoint(path: Path, spec: ModelSpec, model: MLPPolicy, metrics: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "spec": asdict(spec),
            "state_dict": model.state_dict(),
            "metrics": metrics,
        },
        path,
    )


def load_checkpoint(path: Path) -> tuple[ModelSpec, MLPPolicy, dict]:
    checkpoint = torch.load(path, map_location="cpu")
    spec = ModelSpec(**checkpoint["spec"])
    model = MLPPolicy(spec.input_dim, spec.output_dim, spec.hidden_dim, spec.continuous)
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    return spec, model, checkpoint.get("metrics", {})
