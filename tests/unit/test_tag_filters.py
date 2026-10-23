import polars as pl

from logdash.analyses.tags import TAGS_SCHEMA
from logdash.app import tag_filters as tf
from logdash.cache.store import CacheStore
from logdash.tagging.model import COMPONENTS


def framed() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "template_id": [1, 2, 3, 4],
            "tagged": [True, True, True, False],
            "actionable": [False, True, False, None],
            "component": ["network", "network", "storage", None],
            "noise": [True, False, False, None],
            "summary": ["a", "b", "c", None],
        }
    )


def ids(df: pl.DataFrame) -> list[int]:
    return df["template_id"].to_list()


def test_tag_filter_from_controls():
    assert tf.tag_filter(None, None, None) == tf.TagFilter()
    assert not tf.tag_filter(None, None, None).active
    f = tf.tag_filter(True, False, ["network", "bogus"])
    assert f.components == ("network",) and f.hide_noise and f.active


def test_apply_tag_filter_semantics():
    df = framed()
    assert ids(tf.apply_tag_filter(df, tf.TagFilter())) == [1, 2, 3, 4]
    assert ids(tf.apply_tag_filter(df, tf.TagFilter(hide_noise=True))) == [2, 3, 4]
    assert ids(tf.apply_tag_filter(df, tf.TagFilter(actionable_only=True))) == [2]
    assert ids(tf.apply_tag_filter(df, tf.TagFilter(components=("storage",)))) == [3]
    both = tf.TagFilter(hide_noise=True, components=("network",))
    assert ids(tf.apply_tag_filter(df, both)) == [2]


def tag_row(tid: int, text: str, noise: bool = False) -> dict:
    return {
        "template_id": tid,
        "template": text,
        "tagged": True,
        "actionable": True,
        "component": "network",
        "noise": noise,
        "summary": f"about {text}",
    }


def test_attach_tags_matches_on_text():
    tags = pl.DataFrame([tag_row(1, "a"), tag_row(2, "b", True)], schema=TAGS_SCHEMA)
    templates = pl.DataFrame({"template_id": [7, 8, 9], "template": ["b", "a", "new"]})
    out = tf.attach_tags(templates, tags)
    assert out["template_id"].to_list() == [7, 8, 9]
    assert out["summary"].to_list() == ["about b", "about a", None]
    assert out["noise"].to_list() == [True, False, None]
    assert out["tagged"].to_list() == [True, True, False]
    dup = pl.concat([tags, pl.DataFrame([tag_row(3, "a")], schema=TAGS_SCHEMA)])
    assert tf.attach_tags(templates, dup).height == 3


def test_load_tags(tmp_path):
    store = CacheStore(tmp_path)
    assert tf.load_tags(store) is None
    store.write_json("tags", "meta.json", {"available": False, "reason": "no key"})
    assert tf.load_tags(store) is None
    untagged = pl.DataFrame([{**tag_row(1, "a"), "tagged": False}], schema=TAGS_SCHEMA)
    store.write_parquet("tags", "tags.parquet", untagged)
    assert tf.load_tags(store) is None
    one = pl.DataFrame([tag_row(1, "a")], schema=TAGS_SCHEMA)
    store.write_parquet("tags", "tags.parquet", one)
    assert tf.load_tags(store) is not None


def test_tag_controls_and_cells():
    shown = tf.tag_controls("p", True)
    assert shown.id == "p-tag-filters" and getattr(shown, "style", None) is None
    assert [c.id for c in shown.children] == ["p-hide-noise", "p-actionable-only", "p-components"]
    assert shown.children[2].data == list(COMPONENTS)
    assert tf.tag_controls("p", False).style == {"display": "none"}
    row = {"actionable": True, "component": "network", "noise": False, "summary": "s"}
    assert tf.tag_cells(row) == {
        "actionable": "yes",
        "component": "network",
        "noise": "no",
        "summary": "s",
    }
    blank = {"actionable": None, "component": None, "noise": None, "summary": None}
    assert tf.tag_cells(blank) == {"actionable": "", "component": "", "noise": "", "summary": None}
    assert [c["field"] for c in tf.tag_column_defs()] == ["actionable", "component", "noise"]
