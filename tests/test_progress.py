from ico_mcp.progress import ProgressReporter, Status
from ico_mcp.protocol import JsonObject


def snapshot(key: str, label: str, done: float, total: float) -> JsonObject:
    return {
        "key": key,
        "label": label,
        "done": done,
        "total": total,
        "rate": None,
        "eta": None,
    }


def test_first_node_is_the_status() -> None:
    reporter = ProgressReporter()

    statuses = [
        reporter.update(snapshot("0", "epoch", 0, 10)),
        reporter.update(snapshot("1", "batch", 0, 900)),
        reporter.update(snapshot("1", "batch", 450, 900)),
        reporter.update(snapshot("0", "epoch", 1, 10)),
    ]

    assert statuses == [
        Status(0, 10, "epoch 0/10"),
        None,
        None,
        Status(1, 10, "epoch 1/10"),
    ]


def test_progress_only_increases() -> None:
    reporter = ProgressReporter()

    reporter.update(snapshot("0", "items", 3, 3))
    restarted = reporter.update(snapshot("0", "items", 1, 3))
    same = reporter.update(snapshot("0", "items", 3, 3))
    more = reporter.update(snapshot("0", "items", 4, 3))

    assert restarted is None
    assert same is None
    assert more == Status(4, 3, "items 4/3")


def test_unknown_total() -> None:
    reporter = ProgressReporter()

    assert reporter.update(snapshot("0", "items", 5, 0)) == Status(5, None, "items 5")


def test_summary_keeps_the_last_state_of_every_node() -> None:
    reporter = ProgressReporter()
    reporter.update(snapshot("0", "epoch", 0, 2))
    reporter.update(snapshot("1", "batch", 0, 4))
    reporter.update(snapshot("1", "batch", 4, 4))
    reporter.update(snapshot("0", "epoch", 2, 2))

    assert reporter.summary() == [
        {"key": "0", "label": "epoch", "done": 2, "total": 2},
        {"key": "1", "label": "batch", "done": 4, "total": 4},
    ]
