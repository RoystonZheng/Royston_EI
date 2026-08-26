"""Train the narrow-passage RRT comparison policy."""

from __future__ import annotations

from train_common import brief_result, train_task


if __name__ == "__main__":
    print(brief_result(train_task("rrt_narrow", difficulty="all", epochs=220)))
