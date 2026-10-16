(function () {
  "use strict";
  var TYPES = ["follow", "subscribe", "gift", "resub", "cheer", "raid", "stream_online", "stream_offline", "channel_update"];
  var feed = document.getElementById("feed");
  var params = new URLSearchParams(location.search);
  var n = parseInt(params.get("max"), 10);
  var MAX = isNaN(n) ? 8 : Math.max(2, Math.min(50, n));
  var only = (params.get("types") || "").split(",").filter(function (t) { return TYPES.indexOf(t) >= 0; });
  var size = parseInt(params.get("size"), 10);
  if (!isNaN(size)) document.body.style.fontSize = Math.max(10, Math.min(120, size)) + "px";

  function add(m) {
    if (!m || TYPES.indexOf(m.type) < 0 || (only.length && only.indexOf(m.type) < 0)) return;
    var el = document.createElement("div");
    el.className = "ev";
    el.dataset.type = m.type;
    var kind = document.createElement("span");
    kind.className = "kind";
    kind.textContent = String(m.label || m.type);
    var text = document.createElement("span");
    text.textContent = String(m.text || "");
    el.appendChild(kind);
    el.appendChild(text);
    feed.appendChild(el);
    while (feed.children.length > MAX) feed.removeChild(feed.firstChild);
  }

  var es = new EventSource("/events/stream");
  es.onmessage = function (e) {
    var m;
    try { m = JSON.parse(e.data); } catch (err) { return; }
    add(m);
  };
})();
