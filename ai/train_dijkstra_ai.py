"""Train the Dijkstra comparison policy."""

from __future__ import annotations

from train_common import train_task


if __name__ == "__main__":
    print(train_task("dijkstra", difficulty="all", epochs=180))
