// The server renders the pages. This script fetches a page of a run that is still going again every few seconds,
// and handles the few things a click does: select, post.
(function () {
  "use strict";
  var part = document.getElementById("live-part");  // everything but the sidebar; the server can render it alone

  // ---- the page, rendered again ----
  var timer = null;   // a refresh is waiting to start
  var busy = false;   // a refresh is being fetched
  var dirty = false;  // something happened that the page does not show yet
  function refresh(wait) {
    if (document.hidden || busy) { dirty = true; return; }  // a tab nobody looks at waits; one fetch at a time
    if (timer) return;
    timer = setTimeout(function () {
      timer = null;
      if (document.hidden) { dirty = true; return; }
      busy = true;
      // the address as it is now, so the selected file or span stays selected
      fetch(location.pathname + location.search + (location.search ? "&" : "?") + "fragment=1", { headers: { Accept: "text/html" } })
        .then(function (r) { return r.ok ? r.text() : Promise.reject(r.status); })
        .then(function (html) {
          var open = {};  // the rows and folders a person opened or closed stay that way
          part.querySelectorAll("details[data-id]").forEach(function (d) { open[d.dataset.id] = d.open; });
          part.innerHTML = html;
          if (!part.querySelector(".bar[data-live]")) part.removeAttribute("data-refresh");  // the run has ended: nothing more to fetch
          part.querySelectorAll("details[data-id]").forEach(function (d) { if (d.dataset.id in open) d.open = open[d.dataset.id]; });
          var bar = part.querySelector(".bar[data-running]"), badge = document.getElementById("running");
          if (bar && badge) { badge.textContent = bar.dataset.running; badge.hidden = bar.dataset.running === "0"; }
        })
        .catch(function () {})  // the next round or click tries again
        .finally(function () {
          busy = false;
          if (dirty) { dirty = false; refresh(); }  // once more, for what happened during the fetch
        });
    }, wait == null ? 1000 : wait);
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && dirty) { dirty = false; refresh(0); }  // back in view: show what was skipped
  });

  // ---- a page of a run that is still going: fetched again every data-refresh seconds, while someone looks at it ----
  if (part && part.dataset.refresh) setInterval(function () { if (part.dataset.refresh) refresh(0); }, 1000 * Number(part.dataset.refresh));

  document.addEventListener("click", function (ev) {
    // ---- the trace: a click on a row shows that span in the pane; the arrow opens and closes it ----
    var row = ev.target.closest(".sp-row[data-span]");
    if (row && !ev.target.closest("a.go")) {
      if (!ev.target.closest(".tw") && ev.detail) ev.preventDefault();  // selecting neither follows the row's link nor folds it; the keyboard does both
      var id = row.dataset.span;
      document.querySelectorAll(".sp-row.on").forEach(function (r) { r.classList.remove("on"); });
      row.classList.add("on");
      document.querySelectorAll(".sp-info").forEach(function (s) { s.hidden = s.dataset.for !== id; });
      history.replaceState(null, "", "?span=" + encodeURIComponent(id));  // a reload, or the live refresh, keeps the selection
      return;
    }
    var all = ev.target.closest("button[data-trace]");
    if (all) {
      document.querySelectorAll(".trace details").forEach(function (d) { d.open = all.dataset.trace === "open"; });
      return;
    }

    // ---- buttons that change something: one POST, with the header the server asks for ----
    var b = ev.target.closest("button[data-post]");
    if (!b) return;
    if (b.dataset.confirm && !window.confirm(b.dataset.confirm)) return;
    b.disabled = true;
    fetch(b.dataset.post, { method: "POST", headers: { "Content-Type": "application/json", "X-Agentflow": "1" }, body: b.dataset.body || "{}" })
      .then(function (r) {
        return r.text().then(function (t) {
          var answer = {};
          try { answer = JSON.parse(t); } catch (e) { answer = { detail: t }; }
          if (!r.ok) window.alert("That did not work: " + (answer.detail || t));
          else if (answer.go) window.location.href = answer.go;
        });
      })
      .catch(function () { window.alert("The server could not be reached."); })
      .finally(function () { b.disabled = false; refresh(250); });
  });
})();
