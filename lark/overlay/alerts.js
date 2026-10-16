(function () {
  "use strict";
  var TYPES = ["follow", "subscribe", "gift", "resub", "cheer", "raid", "stream_online", "stream_offline", "channel_update"];
  var SAMPLES = [
    ["follow", "New follower", "TestViewer just followed"],
    ["subscribe", "New subscriber", "TestViewer subscribed"],
    ["gift", "Gifted subs", "TestViewer gifted 5 subs"],
    ["resub", "Resubscribed", "TestViewer resubscribed for 12 months"],
    ["cheer", "Bits", "TestViewer cheered 500 bits"],
    ["raid", "Incoming raid", "TestRaider is raiding with 42 viewers"]
  ];
  var params = new URLSearchParams(location.search);
  var box = document.getElementById("alert");
  var titleEl = document.getElementById("title");
  var textEl = document.getElementById("text");
  var queue = [];
  var busy = false;
  var MAX_QUEUE = 100;

  function clamp(v, lo, hi, d) {
    var n = parseFloat(v);
    return isNaN(n) ? d : Math.max(lo, Math.min(hi, n));
  }

  function play(url, volume) {
    if (!/^\/alerts\/sound\/[a-z_]+\?v=\d+$/.test(url || "")) return;
    var a = new Audio(url);
    a.volume = clamp(volume, 0, 1, 0.8);
    var p = a.play();
    if (p && p.catch) p.catch(function () {});
  }

  function next() {
    if (busy || !queue.length) return;
    busy = true;
    var a = queue.shift();
    box.dataset.type = TYPES.indexOf(a.type) >= 0 ? a.type : "follow";
    titleEl.textContent = String(a.title || "");
    textEl.textContent = String(a.text || "");
    play(a.sound, a.volume);
    requestAnimationFrame(function () { requestAnimationFrame(function () { box.classList.add("show"); }); });
    setTimeout(function () {
      box.classList.remove("show");
      setTimeout(function () { busy = false; next(); }, 700);
    }, clamp(a.duration, 1, 60, 6) * 1000);
  }

  function enqueue(a) {
    queue.push(a);
    while (queue.length > MAX_QUEUE) queue.shift();
    next();
  }

  if (params.get("test") === "1") {
    document.body.classList.add("guide");
    var i = 0;
    var tick = function () {
      var s = SAMPLES[i++ % SAMPLES.length];
      enqueue({ type: s[0], title: s[1], text: s[2], duration: 3 });
    };
    tick();
    setInterval(tick, 5000);
  } else {
    var es = new EventSource("/alerts/stream");
    es.onmessage = function (e) {
      var m;
      try { m = JSON.parse(e.data); } catch (err) { return; }
      if (m && typeof m === "object") enqueue(m);
    };
  }
})();
