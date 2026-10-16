(function () {
  "use strict";
  var titleEl = document.getElementById("title");
  var countEl = document.getElementById("count");
  var end = null;
  var offset = 0;

  function fmt(s) {
    s = Math.max(0, Math.ceil(s));
    var m = Math.floor(s / 60), r = s % 60;
    return m + ":" + (r < 10 ? "0" : "") + r;
  }

  function tick() {
    if (end === null) { countEl.hidden = true; return; }
    countEl.hidden = false;
    var left = end - (Date.now() / 1000 + offset);
    countEl.textContent = left > 0 ? fmt(left) : "Any second now";
  }

  function poll() {
    fetch("/starting/state").then(function (r) { return r.json(); }).then(function (s) {
      titleEl.textContent = s.title || "Starting soon";
      end = typeof s.end === "number" ? s.end : null;
      offset = typeof s.now === "number" ? s.now - Date.now() / 1000 : 0;
      tick();
    }).catch(function () {});
  }

  poll();
  setInterval(poll, 2000);
  setInterval(tick, 250);
})();
