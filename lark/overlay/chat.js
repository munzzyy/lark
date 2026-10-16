(function () {
  "use strict";
  var feed = document.getElementById("feed");
  var params = new URLSearchParams(location.search);
  var MAX = clampInt(params.get("max"), 3, 50, 14);
  var fade = null;
  var SHOW_BADGES = { moderator: "mod", broadcaster: "host", vip: "vip", subscriber: "sub" };

  function clampInt(v, lo, hi, dflt) {
    var n = parseInt(v, 10);
    return isNaN(n) ? dflt : Math.max(lo, Math.min(hi, n));
  }
  if (params.get("size")) document.body.style.fontSize = clampInt(params.get("size"), 10, 120, 26) + "px";

  function removeNode(el) {
    el.classList.add("out");
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 700);
  }

  function add(m) {
    var el = document.createElement("div");
    el.className = "msg" + (m.action ? " action" : "");
    el.dataset.id = m.id || "";
    el.dataset.user = (m.user || "").toLowerCase();
    (m.badges || []).forEach(function (b) {
      if (SHOW_BADGES[b]) {
        var bd = document.createElement("span");
        bd.className = "badge";
        bd.textContent = SHOW_BADGES[b];
        el.appendChild(bd);
      }
    });
    var name = document.createElement("span");
    name.className = "name";
    name.textContent = m.user || "";
    if (/^#[0-9a-fA-F]{6}$/.test(m.color || "")) name.style.color = m.color;
    var text = document.createElement("span");
    text.className = "text";
    text.textContent = m.text || "";
    el.appendChild(name);
    el.appendChild(text);
    feed.appendChild(el);
    while (feed.children.length > MAX) feed.removeChild(feed.firstChild);
    var secs = fadeSeconds();
    if (secs > 0) {
      var age = Math.max(0, Date.now() / 1000 - (m.ts || 0));
      setTimeout(function () { removeNode(el); }, Math.max(0, secs - age) * 1000);
    }
  }

  function fadeSeconds() {
    var q = params.get("fade");
    if (q !== null) return clampInt(q, 0, 3600, 30);
    return fade === null ? 30 : fade;
  }

  function remove(d) {
    Array.prototype.slice.call(feed.children).forEach(function (el) {
      if ((d.id && el.dataset.id === d.id) || (d.user && el.dataset.user === d.user.toLowerCase()) ||
          (d.id == null && d.user == null)) {
        if (el.parentNode) el.parentNode.removeChild(el);
      }
    });
  }

  function start() {
    var es = new EventSource("/chat/events");
    es.onmessage = function (e) {
      var m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m.delete) remove(m.delete); else add(m);
    };
  }

  fetch("/chat/config").then(function (r) { return r.json(); }).then(function (c) {
    fade = typeof c.fade === "number" ? c.fade : 30;
  }).catch(function () { fade = 30; }).then(start);
})();
