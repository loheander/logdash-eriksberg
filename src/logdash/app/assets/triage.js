/* Clientside half of the triage page (analyses/triage.py).
 *
 * select: a grid row, a scatter point or "ask again" -> {key, n}; n counts the calls.
 * open:   open the drawer at once from the row data, dim the answers and start the timer.
 * tick:   the live timer while the request waits.
 * arrive: the server's result -> timing, error and the answer values computed in Python;
 *         a result for another call than the selected one is ignored.
 * count:  count the numbers up from their previous values over the same 250 ms.
 * judged: put the session's severities and subsystems into the grid rows.
 */
(function () {
  var FIXED = 9; // fixed outputs of arrive before the view outputs (triage.FIXED_OUTPUTS)
  var DURATION = 250;
  var DIM = {opacity: 0.35, transition: "opacity 250ms ease-out"};
  var SHOWN = {opacity: 1, transition: "opacity 250ms ease-out"};
  var HIDDEN = {display: "none"};

  function dc() {
    return window.dash_clientside;
  }

  function same(n, value) {
    var out = [];
    for (var i = 0; i < n; i++) out.push(value);
    return out;
  }

  function ms(value) {
    return Math.round(value) + " ms";
  }

  var triage = {
    select: function (cell, click, again, selected) {
      var triggered = (dc().callback_context.triggered || []).map(function (t) {
        return t.prop_id.split(".")[0];
      });
      var key = null;
      if (triggered.indexOf("triage-grid") >= 0 && cell && cell.rowId) {
        key = cell.rowId;
      } else if (triggered.indexOf("triage-scatter") >= 0 && click && click.points &&
                 click.points.length && click.points[0].customdata) {
        key = click.points[0].customdata;
      } else if (triggered.indexOf("triage-again") >= 0 && selected && selected.key) {
        key = selected.key;
      }
      if (!key) return dc().no_update;
      return {key: key, n: ((selected && selected.n) || 0) + 1};
    },

    open: function (selected, rows, config) {
      if (!selected || !selected.key) return same(14, dc().no_update);
      var row = (rows || []).find(function (r) { return r.row_key === selected.key; }) || {};
      window.__triage = {key: selected.key, n: selected.n, t0: performance.now(), done: false};
      return [
        true,
        row.head || "",
        row.entity || selected.key,
        row.subsystem || "unknown",
        row.when || "",
        row.score_text || "",
        row.evidence || "",
        row.href || "/explorer",
        false,
        "asking " + config.model + " … 0.00 s",
        "",
        HIDDEN,
        "",
        DIM,
      ];
    },

    tick: function (n, config) {
      var call = window.__triage;
      if (!call || call.done) return dc().no_update;
      var seconds = (performance.now() - call.t0) / 1000;
      return "asking " + config.model + " … " + seconds.toFixed(2) + " s";
    },

    arrive: function (result, selected, session, config) {
      var nu = dc().no_update;
      var total = FIXED + config.view_n;
      if (!result || !selected || result.key !== selected.key || result.n !== selected.n) {
        return same(total, nu);
      }
      var call = window.__triage;
      var trip = call && call.key === result.key && call.n === result.n ?
        performance.now() - call.t0 : null;
      if (call) call.done = true;
      var parts = [];
      if (result.state_ms != null) parts.push("state " + ms(result.state_ms));
      if (result.jev_ms != null) parts.push("jev " + ms(result.jev_ms));
      if (trip != null) {
        var other = trip - (result.state_ms || 0) - (result.jev_ms || 0);
        parts.push("other " + ms(Math.max(0, other)));
      }
      if (result.cost != null) parts.push("$" + result.cost.toPrecision(2));
      var timer = (result.error ? "failed after " : "answered in ") +
        (trip != null ? (trip / 1000).toFixed(2) + " s" : "");
      var head = [
        true,
        timer,
        parts.join(" · "),
        result.error ? {} : HIDDEN,
        result.error || "",
        result.error ? HIDDEN : SHOWN,
        result.subsystem || nu,
      ];
      if (result.error || !result.view) {
        return head.concat([nu, nu], same(config.view_n, nu));
      }
      var judged = Object.assign({}, session || {});
      judged[result.key] = {severity: result.severity, subsystem: result.subsystem || null};
      return head.concat([judged, result.numbers], result.view);
    },

    count: function (numbers) {
      if (!numbers) return dc().no_update;
      var previous = window.__triageNumbers || {};
      var start = null;
      var ease = function (t) { return 1 - Math.pow(1 - t, 3); };
      var frame = function (now) {
        if (start === null) start = now;
        var t = Math.min(1, (now - start) / DURATION);
        Object.keys(numbers).forEach(function (id) {
          var el = document.getElementById(id);
          if (!el) return;
          var to = numbers[id][0];
          var from = previous[id] == null ? 0 : previous[id];
          el.textContent = (from + (to - from) * ease(t)).toFixed(numbers[id][1]);
        });
        if (t < 1) window.requestAnimationFrame(frame);
      };
      window.requestAnimationFrame(frame);
      var next = {};
      Object.keys(numbers).forEach(function (id) { next[id] = numbers[id][0]; });
      window.__triageNumbers = next;
      return Object.keys(next).length;
    },

    judged: function (session, rows) {
      if (!session || !rows) return dc().no_update;
      var update = rows.filter(function (r) {
        var e = session[r.row_key];
        if (!e || typeof e !== "object" || typeof e.severity !== "number") return false;
        return r.judged !== e.severity || (e.subsystem && r.subsystem !== e.subsystem);
      }).map(function (r) {
        var e = session[r.row_key];
        return Object.assign({}, r, {judged: e.severity, subsystem: e.subsystem || r.subsystem});
      });
      return update.length ? {update: update} : dc().no_update;
    },
  };

  window.dash_clientside = Object.assign({}, window.dash_clientside, {triage: triage});
})();
