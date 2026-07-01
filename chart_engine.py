# -*- coding: utf-8 -*-
"""
Universal Plotly figure builder for the SpliceScout Assistant.

Turns a list of row-dicts (typically a SQL result over ANY of a run's data — studies, samples,
study_protocol, pipeline_stages, the data_funnel, or any tables/runtable CSV) plus a high-level spec
into a Plotly figure {"data":[...], "layout":{...}} that the browser renders verbatim
(server.py passes fig.data / fig.layout straight to Plotly.newPlot). It is data-source-agnostic:
whatever the assistant can query, it can chart here.

Supported chart types: bar / hbar (+ grouped or stacked via `color`), line, area, scatter,
histogram, box, violin, pie, funnel, waterfall, heatmap. Numeric columns are auto-detected; an
optional `agg` (count/sum/mean/min/max) groups rows by `x` (and `color`) so the assistant can chart
raw rows without pre-grouping in SQL. Pure / stdlib-only / read-only — returns (fig, None) on success
or (None, "message") with guidance on failure.
"""
from collections import OrderedDict

# chart types the vendored full Plotly bundle can render
TYPES = ("bar", "hbar", "line", "area", "scatter", "histogram", "box", "violin",
         "pie", "funnel", "waterfall", "heatmap")
# which types group/aggregate one value per category (the rest plot raw points/values)
_AGG_TYPES = ("bar", "hbar", "line", "area", "pie", "funnel", "waterfall")


def _num(v):
    """Coerce a cell to float, or None. Bools are not numbers here."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _s(v):
    return "" if v is None else str(v)


def _numeric_cols(rows):
    """Columns where >=60% of non-empty values parse as numbers."""
    out = set()
    if not rows:
        return out
    for c in rows[0].keys():
        ne = [r.get(c) for r in rows]
        ne = [v for v in ne if v not in (None, "")]
        if ne and sum(1 for v in ne if _num(v) is not None) >= 0.6 * len(ne):
            out.add(c)
    return out


def _reduce(vals, how):
    vals = [v for v in vals if v is not None]
    if how == "count":
        return float(len(vals))
    if not vals:
        return 0.0
    if how == "sum":
        return float(sum(vals))
    if how == "mean":
        return sum(vals) / len(vals)
    if how == "min":
        return float(min(vals))
    if how == "max":
        return float(max(vals))
    return float(sum(vals))


def _aggregate(rows, xkey, ykey, how, colorkey):
    """Group rows by x (and color) and reduce y. Returns (categories, {color: {cat: value}})."""
    cats = OrderedDict()
    series = OrderedDict()
    for r in rows:
        cat = _s(r.get(xkey))
        cats[cat] = True
        cv = _s(r.get(colorkey)) if colorkey else ""
        bucket = series.setdefault(cv, {}).setdefault(cat, [])
        bucket.append(1.0 if ykey is None else _num(r.get(ykey)))
    how2 = "count" if ykey is None else (how or "sum")
    out = OrderedDict()
    for cv, cm in series.items():
        out[cv] = {c: _reduce(cm.get(c, []), how2) for c in cats}
    return list(cats.keys()), out


def _order_cats(cats, series, sort):
    """Optionally reorder categories by name (sort='x') or by the single series' value (sort='y'/'-y')."""
    if sort == "x":
        nums = [_num(c) for c in cats]
        if cats and all(n is not None for n in nums):
            return [c for _, c in sorted(zip(nums, cats))]
        return sorted(cats)
    if sort in ("y", "-y") and len(series) == 1:
        only = next(iter(series.values()))
        return sorted(cats, key=lambda c: only.get(c, 0), reverse=(sort == "-y"))
    return cats


def _layout(spec, **extra):
    lay = {"title": spec.get("title") or "", "template": "plotly_white", "height": 430,
           "margin": {"t": 46, "r": 16, "b": 80, "l": 70}}
    lay.update(extra)
    return lay


def build_figure(rows, spec):
    """rows: list[dict]; spec keys: type, x, y, color, agg, title, orientation, sort, colorscale, z.
    Returns (fig_dict, None) on success or (None, error_message)."""
    if not rows:
        return None, "no rows to chart (the query/source returned nothing)."
    spec = spec or {}
    cols = list(rows[0].keys())
    numeric = _numeric_cols(rows)
    t = (spec.get("type") or "").lower().strip()
    x = spec.get("x") or None
    y = spec.get("y") or None
    color = spec.get("color") or None
    agg = (spec.get("agg") or "").lower().strip() or None
    orient = (spec.get("orientation") or "").lower().strip()
    sort = (spec.get("sort") or "").lower().strip()

    def colvals(k):
        return [r.get(k) for r in rows]

    # default x to the first column if the model didn't say
    if not x and cols:
        x = cols[0]
    # infer a chart type when not given
    if not t:
        if x in numeric and y in numeric:
            t = "scatter"
        elif x and y:
            t = "bar"
        elif x in numeric:
            t = "histogram"
        elif x:
            t = "bar"
        else:
            return None, "specify at least x (a column name) — call list_data to see columns."
    if t == "hbar":
        t, orient = "bar", "h"
    if t not in TYPES:
        return None, "unknown chart type %r. Use one of: %s." % (t, ", ".join(TYPES))

    # ---- histogram: distribution of one numeric column ----
    if t == "histogram":
        xs = [v for v in (_num(v) for v in colvals(x)) if v is not None]
        if not xs:
            return None, "histogram needs a numeric x column; %r isn't numeric." % x
        return {"data": [{"type": "histogram", "x": xs}],
                "layout": _layout(spec, xaxis={"title": x}, yaxis={"title": "count"})}, None

    # ---- scatter: numeric vs numeric, optional color series ----
    if t == "scatter":
        if x not in numeric or not y:
            return None, "scatter needs numeric x and y. Numeric columns: %s." % ", ".join(sorted(numeric))
        if color:
            groups = OrderedDict()
            for r in rows:
                groups.setdefault(_s(r.get(color)) or "—", []).append(r)
            data = [{"type": "scatter", "mode": "markers", "name": g,
                     "x": [_num(r.get(x)) for r in rs], "y": [_num(r.get(y)) for r in rs]}
                    for g, rs in groups.items()]
        else:
            data = [{"type": "scatter", "mode": "markers",
                     "x": [_num(v) for v in colvals(x)], "y": [_num(v) for v in colvals(y)]}]
        return {"data": data, "layout": _layout(spec, xaxis={"title": x}, yaxis={"title": y})}, None

    # ---- box / violin: distribution of a numeric y per categorical x ----
    if t in ("box", "violin"):
        yk = y if y in numeric else (x if x in numeric else None)
        if not yk:
            return None, "%s needs a numeric column for the distribution." % t
        xk = x if x != yk else None
        if xk:
            groups = OrderedDict()
            for r in rows:
                groups.setdefault(_s(r.get(xk)) or "—", []).append(_num(r.get(yk)))
            data = [{"type": t, "name": g, "y": [v for v in vs if v is not None]}
                    for g, vs in groups.items()]
        else:
            data = [{"type": t, "y": [v for v in (_num(v) for v in colvals(yk)) if v is not None]}]
        return {"data": data, "layout": _layout(spec, yaxis={"title": yk})}, None

    # ---- everything below aggregates one value per category ----
    cats, series = _aggregate(rows, x, y, agg, color)
    cats = _order_cats(cats, series, sort)

    # ---- pie: labels + values ----
    if t == "pie":
        single = next(iter(series.values()))
        return {"data": [{"type": "pie", "labels": cats, "values": [single.get(c, 0) for c in cats]}],
                "layout": _layout(spec)}, None

    # ---- funnel: ordered steps (keep row order), one value each ----
    if t == "funnel":
        single = next(iter(series.values()))
        vals = [single.get(c, 0) for c in cats]
        return {"data": [{"type": "funnel", "y": cats, "x": vals, "textinfo": "value+percent initial"}],
                "layout": _layout(spec, margin={"t": 46, "r": 16, "b": 40, "l": 180})}, None

    # ---- waterfall: first absolute, rest as deltas (shows what each step adds/removes) ----
    if t == "waterfall":
        single = next(iter(series.values()))
        vals = [single.get(c, 0) for c in cats]
        measure = ["absolute"] + ["relative"] * (len(vals) - 1)
        deltas = [vals[0]] + [vals[i] - vals[i - 1] for i in range(1, len(vals))]
        return {"data": [{"type": "waterfall", "x": cats, "y": deltas, "measure": measure}],
                "layout": _layout(spec, xaxis={"title": x}, yaxis={"title": y or "count"})}, None

    # ---- line / area ----
    if t in ("line", "area"):
        data = []
        for cv, cm in series.items():
            tr = {"type": "scatter", "mode": "lines+markers", "x": cats, "y": [cm.get(c, 0) for c in cats]}
            if cv:
                tr["name"] = cv
            if t == "area":
                tr["fill"] = "tozeroy"
            data.append(tr)
        return {"data": data, "layout": _layout(spec, xaxis={"title": x},
                                                yaxis={"title": y or "count"})}, None

    # ---- bar (single, grouped, or stacked) ----
    data = []
    for cv, cm in series.items():
        vals = [cm.get(c, 0) for c in cats]
        tr = {"type": "bar"}
        if orient == "h":
            tr["orientation"] = "h"
            tr["y"], tr["x"] = cats, vals
        else:
            tr["x"], tr["y"] = cats, vals
        if cv:
            tr["name"] = cv
        data.append(tr)
    lay = _layout(spec)
    if len(series) > 1:
        lay["barmode"] = "stack" if str(spec.get("stack")).lower() in ("1", "true", "yes") else "group"
    axis_cat, axis_val = ({"title": x}, {"title": y or "count"})
    if orient == "h":
        lay["xaxis"], lay["yaxis"] = axis_val, axis_cat
    else:
        lay["xaxis"], lay["yaxis"] = axis_cat, axis_val
    return {"data": data, "layout": lay}, None
