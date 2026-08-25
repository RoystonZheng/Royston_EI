"""Train the car-control comparison policy."""

from __future__ import annotations

from train_common import train_task


if __name__ == "__main__":
    print(train_task("car_control", difficulty="all", epochs=220))
