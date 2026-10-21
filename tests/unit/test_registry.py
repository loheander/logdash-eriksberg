import pytest

from logdash.analyses import base
from logdash.analyses.base import (
    DependencyCycleError,
    DuplicateKeyError,
    MissingDependencyError,
    Registry,
    UnknownAnalysisError,
)


class Dummy:
    title = "Dummy"
    version = "1"

    def __init__(self, key, *deps):
        self.key, self.depends_on = key, tuple(deps)

    def compute(self, ctx):
        pass

    def layout(self, store):
        return None

    def register_callbacks(self, app, store):
        pass


def make(*specs):
    reg = Registry()
    for key, *deps in specs:
        reg.add(Dummy(key, *deps))
    return reg


def keys(analyses):
    return [a.key for a in analyses]


def test_protocol_runtime_check():
    assert isinstance(Dummy("a"), base.Analysis)
    assert not isinstance(object(), base.Analysis)


def test_topological_order():
    reg = make(("pca", "features"), ("features",), ("anomalies", "pca", "features"), ("temporal",))
    order = keys(reg.ordered())
    assert order == ["features", "pca", "anomalies", "temporal"]
    assert len(reg) == 4 and "pca" in reg and "zzz" not in reg


def test_ordered_subset_includes_dependencies():
    reg = make(("a",), ("b", "a"), ("c", "b"), ("d",))
    assert keys(reg.ordered(["c"])) == ["a", "b", "c"]
    assert keys(reg.ordered([])) == []


def test_cycle_detected():
    reg = make(("a", "b"), ("b", "c"), ("c", "a"))
    with pytest.raises(DependencyCycleError, match="a -> b -> c -> a"):
        reg.ordered()


def test_missing_dependency():
    reg = make(("a", "ghost"))
    with pytest.raises(MissingDependencyError, match="ghost"):
        reg.ordered()
    with pytest.raises(MissingDependencyError):
        reg.ordered(["a"])


def test_duplicate_reserved_and_unknown():
    reg = make(("a",))
    with pytest.raises(DuplicateKeyError):
        reg.add(Dummy("a"))
    with pytest.raises(DuplicateKeyError):
        reg.add(Dummy("events"))
    with pytest.raises(UnknownAnalysisError):
        reg.get("nope")
    assert reg.get("a").key == "a"
    reg.clear()
    assert len(reg) == 0


def test_register_decorator():
    saved = dict(base.REGISTRY._items)
    base.REGISTRY.clear()
    try:

        @base.register
        class Mine(Dummy):
            def __init__(self):
                super().__init__("mine")

        assert Mine is not None and "mine" in base.REGISTRY
    finally:
        base.REGISTRY.clear()
        base.REGISTRY._items.update(saved)
