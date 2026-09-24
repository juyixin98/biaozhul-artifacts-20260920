"""Shared example instances: crossroad, single-channel corridor, unsolvable."""

CROSSROAD = {
    "grid": {"width": 5, "height": 5, "obstacles": []},
    "agents": [
        {"id": "east", "start": [0, 2], "goal": [4, 2]},
        {"id": "south", "start": [2, 0], "goal": [2, 4]},
    ],
}

# 5x3 map whose middle two columns form a 1-wide corridor; the columns at
# both ends are fully free, giving room to pass, so the swap is solvable.
CORRIDOR_WITH_BAYS = {
    "grid": {
        "width": 5,
        "height": 3,
        "obstacles": [[1, 0], [2, 0], [1, 2], [2, 2]],
    },
    "agents": [
        {"id": "go-east", "start": [0, 1], "goal": [4, 1]},
        {"id": "go-west", "start": [4, 1], "goal": [0, 1]},
    ],
}

# Strict 1x4 corridor: two agents swapping ends can never pass each other.
CORRIDOR_UNSOLVABLE = {
    "grid": {"width": 4, "height": 1, "obstacles": []},
    "agents": [
        {"id": "a", "start": [0, 0], "goal": [3, 0]},
        {"id": "b", "start": [3, 0], "goal": [0, 0]},
    ],
}

ALL = {
    "crossroad": CROSSROAD,
    "corridor_with_bays": CORRIDOR_WITH_BAYS,
    "corridor_unsolvable": CORRIDOR_UNSOLVABLE,
}
