"""Train the Dijkstra comparison policy."""

from __future__ import annotations

from train_common import brief_result, train_task


if __name__ == "__main__":
    print(brief_result(train_task("dijkstra", difficulty="all", epochs=180)))
