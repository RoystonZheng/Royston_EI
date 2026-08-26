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
    def __init__(
        self,
        input_dim: int,
        output_dim: int,
        hidden_dim: int = 96,
        continuous: bool = False,
        orthogonal: bool = True,
    ):
        super().__init__()
        self.continuous = continuous
        self.output_dim = output_dim
        self.net = nn.Sequential(
            nn.Linear(input_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, hidden_dim),
            nn.Tanh(),
            nn.Linear(hidden_dim, output_dim),
        )
        if continuous:
            self.log_std = nn.Parameter(torch.full((output_dim,), -0.8))
        if orthogonal:
            self.apply_orthogonal_initialization()

    def apply_orthogonal_initialization(self) -> None:
        linear_layers = [module for module in self.net if isinstance(module, nn.Linear)]
        for layer in linear_layers[:-1]:
            nn.init.orthogonal_(layer.weight, gain=nn.init.calculate_gain("tanh"))
            nn.init.constant_(layer.bias, 0.0)
        nn.init.orthogonal_(linear_layers[-1].weight, gain=0.01)
        nn.init.constant_(linear_layers[-1].bias, 0.0)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.net(x)
        if self.continuous:
            return torch.tanh(out)
        return out

    def distribution(self, x: torch.Tensor):
        output = self.forward(x)
        if self.continuous:
            std = self.log_std.exp().expand_as(output)
            return torch.distributions.Normal(output, std)
        return torch.distributions.Categorical(logits=output)


def save_checkpoint(
    path: Path,
    spec: ModelSpec,
    model: MLPPolicy,
    metrics: dict,
    normalizer: dict | None = None,
    techniques: list[str] | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    torch.save(
        {
            "spec": asdict(spec),
            "state_dict": model.state_dict(),
            "metrics": metrics,
            "normalizer": normalizer or {},
            "training_techniques": techniques or [],
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
