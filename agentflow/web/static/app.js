// The server renders the pages. This script keeps a page of a run that is still going up to date from the event
// stream, builds the log from that stream, and handles the few things a click does: select, post.
(function () {
  "use strict";
  var part = document.getElementById("live-part");  // everything but the sidebar; the server can render it alone
  var log = document.getElementById("log");
  var QUIET = { tool_call: 1, tool_result: 1, model_call: 1 };  // these change nothing a page shows outside the log

  // ---- the page, rendered again when something happened ----
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
          part.querySelectorAll("details[data-id]").forEach(function (d) { if (d.dataset.id in open) d.open = open[d.dataset.id]; });
          var bar = part.querySelector(".bar[data-running]"), badge = document.getElementById("running");
          if (bar && badge) { badge.textContent = bar.dataset.running; badge.hidden = bar.dataset.running === "0"; }
        })
        .catch(function () {})  // the next event or click tries again
        .finally(function () {
          busy = false;
          if (dirty) { dirty = false; refresh(); }  // once more, for what happened during the fetch
        });
    }, wait == null ? 1000 : wait);
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && dirty) { dirty = false; refresh(0); }  // back in view: show what was skipped
  });

  // ---- one event stream per page, and none once the run has ended ----
  function follow(url, onEvent) {
    var source = new EventSource(url);
    var replay = /[?&]follow=false/.test(url), oneRun = /[?&]run=/.test(url), oneItem = /[?&]item=/.test(url);
    source.onerror = function () { if (replay) source.close(); };  // a replay ends by itself; otherwise the browser reconnects from the last event it saw
    source.onmessage = function (m) {
      var e = JSON.parse(m.data);
      onEvent(e);
      // nothing follows a run's last event, or an item's on that item's page, also when it was replayed
      if (oneRun && (e.type === "run_ended" || (oneItem && e.type === "item_ended"))) source.close();
    };
  }
  // a page with a log has one stream, the log's, and that stream also drives the refresh
  if (part && part.dataset.events && !log) follow(part.dataset.events, function (e) { if (!QUIET[e.type]) refresh(); });

  // ---- the log: built from the stream, so replay and live are the same code ----
  var LINE = Number(document.body.dataset.line);  // how much of a value one log line shows: show.line_chars in the settings
  var stageWho = log ? JSON.parse(log.dataset.who || "{}") : {};  // stage -> who does it, for events that do not say
  var tryBy = {};     // item|stage|try -> who does that try, from its attempt_started
  function who(e) {
    var key = e.item + "|" + e.stage + "|" + e.n;
    if (e.type === "attempt_started") tryBy[key] = e.by === "script" ? (stageWho[e.stage] || "script") : e.by;
    if (e.type === "verdict" || e.type === "person_asked") return e.by;
    if (e.script) return "script " + e.script;
    if (!e.stage || e.type === "stage_passed" || e.type === "sent_back" || e.type === "attempt_interrupted") return "engine";
    if (e.agent) return e.agent;
    return tryBy[key] || stageWho[e.stage] || "engine";
  }
  function took(e) { return e.seconds != null ? " · " + e.seconds + "s" : ""; }
  function line(e) {
    switch (e.type) {
      case "run_started": return ["head", "run started · " + e.flow + " · " + e.items + (e.items === 1 ? " item" : " items") + (e.trial ? " · trial" : "")];
      case "run_ended": return ["head", "run ended"];
      case "run_resumed": return ["head", "run resumed"];
      case "run_interrupted": return ["fail", "run interrupted: " + e.reason];
      case "cancel_requested": return ["fail", "cancel asked for"];
      case "finish_ended": return e.exit_code ? ["fail", "finish script " + e.script + " failed with exit code " + e.exit_code + took(e)] : ["dim", "finish script " + e.script + " ended" + took(e)];
      case "item_started": return ["head", e.from_stage ? "continues at " + e.from_stage : "started"];
      case "item_ended": return ["head", "ended: " + String(e.ended_as).replace(/_/g, " ") + (e.reason ? " · " + e.reason : "")];
      case "attempt_started": return ["head", "try " + e.n + (e.model ? " · " + e.model : "") + (e.feedback ? " · told the earlier reasons" : "")];
      case "attempt_cancelled": return ["fail", "try " + e.n + " cancelled: the script was stopped"];
      case "attempt_interrupted": return ["fail", "try " + e.n + " interrupted: the process stopped"];
      case "tool_call": return ["", "tool " + e.tool + " " + (typeof e.args === "string" ? e.args : JSON.stringify(e.args || {})).slice(0, LINE)];
      case "tool_result": return ["dim", "  " + e.tool + " answered · " + e.chars + " characters" + took(e) + (e.preview ? " · " + String(e.preview).slice(0, LINE) : "")];
      case "model_call": return ["dim", "model call" + (e.model ? " · " + e.model : "") + " · " + e.tokens_in + " in, " + e.tokens_out + " out" + took(e) +
                                 (e.cost_usd == null ? " · cost not recorded" : e.cost_usd === 0 ? " · free" : " · $" + e.cost_usd.toFixed(5) + " estimated")];
      case "cli_run": return ["dim", "the " + e.harness + " CLI ended" + took(e)];
      case "attempt_ended": return ["dim", e.error ? "try failed: " + e.error
                                 : e.exit_code != null ? "exit code " + e.exit_code + took(e) + (e.output ? " · " + String(e.output).slice(-LINE) : "")
                                 : "try ended · " + e.model_calls + " model calls · " + e.tool_calls + " tool calls · " + (e.tokens_in + e.tokens_out) + " tokens" + took(e)];
      case "verdict": return [e.passed ? "pass" : "fail", (e.passed ? "pass" : e.needs_person ? "fail: a person is needed" : "fail") + (e.reason ? " · " + e.reason : "") + took(e)];
      case "stage_passed": return ["pass", "stage passed after " + e.tries + (e.tries === 1 ? " try" : " tries")];
      case "sent_back": return ["fail", "sent back to " + e.to + (e.reason ? " · " + e.reason : "")];
      case "person_asked": return ["fail", "asked for a person: " + e.reason];
      default: return null;  // an event this page has no words for is not shown
    }
  }
  function cell(cls, text) {
    var s = document.createElement("span");
    s.className = cls;
    s.textContent = text;  // model-written text is only ever placed as text
    return s;
  }
  // the log can be narrowed to one item, to lines holding some text, and to lines that are not single calls
  var pickItem = document.getElementById("log-item"), find = document.getElementById("log-find"), showCalls = document.getElementById("log-calls");
  function shown(li) {
    if (pickItem && pickItem.value && li.dataset.item !== pickItem.value) return false;
    if (showCalls && !showCalls.checked && QUIET[li.dataset.type]) return false;
    if (find && find.value && li.textContent.toLowerCase().indexOf(find.value.toLowerCase()) < 0) return false;
    return true;
  }
  function filterLog() { log.querySelectorAll("li").forEach(function (li) { li.hidden = !shown(li); }); }
  [pickItem, find, showCalls].forEach(function (c) { if (c && log) c.addEventListener(c === find ? "input" : "change", filterLog); });

  // new lines wait here and are put on the page together, once per frame
  var waiting = [], drawing = false;
  function draw() {
    drawing = false;
    var atEnd = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    var lines = document.createDocumentFragment();
    waiting.forEach(function (li) { li.hidden = !shown(li); lines.append(li); });
    waiting = [];
    log.append(lines);
    if (atEnd) log.scrollTop = log.scrollHeight;
  }

  if (log) {
    var seen = Number((part && part.dataset.seq) || 0);  // the page already shows everything up to here
    var withItems = !!log.dataset.items;
    follow(log.dataset.events, function (e) {
      var l = line(e);
      if (l) {
        var li = document.createElement("li");
        if (l[0]) li.className = l[0];
        li.dataset.item = e.item || "";
        li.dataset.type = e.type;
        li.append(cell("t", new Date(e.ts * 1000).toLocaleTimeString([], { hour12: false })));
        if (withItems) li.append(cell("i", e.item || "run"));
        li.append(cell("s", e.stage || ""), cell("w", who(e)), cell("m", l[1]));
        waiting.push(li);
        if (!drawing) { drawing = true; requestAnimationFrame(draw); }
      }
      if (e.seq > seen && !QUIET[e.type]) refresh();
    });
  }

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
