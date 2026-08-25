"""Train the dynamic A* comparison policy."""

from __future__ import annotations

from train_common import train_task


if __name__ == "__main__":
    print(train_task("astar_dynamic", difficulty="all", epochs=180))
