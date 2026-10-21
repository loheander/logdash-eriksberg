import numpy as np
import polars as pl

from logdash.app import figures


def test_template_schemes_differ():
    light, dark = figures.template("light"), figures.template(None)
    assert light.layout.font.color != figures.template("dark").layout.font.color
    assert dark.layout.font.color == light.layout.font.color
    assert figures.scheme_name("dark") == "dark" and figures.scheme_name("x") == "light"


def test_style_and_empty_figure():
    fig = figures.empty_figure("no data", "dark")
    assert fig.layout.annotations[0].text == "no data"
    assert figures.axis_type(True) == "log" and figures.axis_type(False) == "linear"


def test_lttb_keeps_endpoints_and_peak():
    x = np.arange(1000)
    y = np.zeros(1000)
    y[500] = 100
    xs, ys = figures.lttb(x, y, 50)
    assert len(xs) == 50 and xs[0] == 0 and xs[-1] == 999 and 500 in xs
    assert np.all(np.diff(xs) > 0)


def test_lttb_passthrough_when_small():
    xs, ys = figures.lttb([1, 2, 3], [4, 5, 6], 10)
    assert xs.tolist() == [1, 2, 3]
    assert len(figures.lttb(range(10), range(10), 2)[0]) == 10


def test_top_n_other():
    df = pl.DataFrame({"k": ["a", "b", "c", "d", "a"], "n": [10, 5, 3, 2, 1]})
    out = figures.top_n_other(df, "k", "n", n=2)
    assert out.to_dict(as_series=False) == {"k": ["a", "b", "other"], "n": [11, 5, 5]}
    assert figures.top_n_other(df, "k", "n", n=10).height == 4


def test_choose_bin_seconds():
    assert figures.choose_bin_seconds(0, 3000) == 1
    assert figures.choose_bin_seconds(0, 86400) == 60
    assert figures.choose_bin_seconds(0, 86400 * 365) == 3600
    assert figures.choose_bin_seconds(5, 1) == 1
