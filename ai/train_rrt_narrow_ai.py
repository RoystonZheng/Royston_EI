"""Train the narrow-passage RRT comparison policy."""

from __future__ import annotations

from train_common import train_task


if __name__ == "__main__":
    print(train_task("rrt_narrow", difficulty="all", epochs=220))
