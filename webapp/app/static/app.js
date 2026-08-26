// Тема
(function () {
  var btn = document.getElementById("theme-toggle");
  if (!btn) return;
  btn.addEventListener("click", function () {
    var cur = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", cur);
    try { localStorage.setItem("theme", cur); } catch (e) {}
  });
})();

// Просмотр плана: наведение на квартиру подсвечивает её и даёт вырезать именно её
(function () {
  var root = document.getElementById("viewer");
  if (!root) return;

  var floorId = root.dataset.floor;
  var plan = document.getElementById("plan");
  var overlay = document.getElementById("overlay");
  var hint = document.getElementById("hint");
  var ctx = overlay.getContext("2d");
  var hit = null, hitCtx = null, hitData = null;
  var flats = {}, cache = {}, active = 0;

  function loadHitmap() {
    var img = new Image();
    img.onload = function () {
      hit = document.createElement("canvas");
      hit.width = img.width; hit.height = img.height;
      hitCtx = hit.getContext("2d", { willReadFrequently: true });
      hitCtx.drawImage(img, 0, 0);
      hitData = hitCtx.getImageData(0, 0, img.width, img.height).data;
      overlay.width = img.width; overlay.height = img.height;
    };
    img.src = "/files/floors/" + floorId + "/hitmap.png?" + Date.now();
  }

  function idAt(x, y) {
    if (!hitData) return 0;
    var i = (Math.floor(y) * hit.width + Math.floor(x)) * 4;
    return hitData[i] || 0;
  }

  function silhouette(id) {
    if (cache[id]) return cache[id];
    var c = document.createElement("canvas");
    c.width = hit.width; c.height = hit.height;
    var cc = c.getContext("2d");
    var img = cc.createImageData(hit.width, hit.height);
    var d = img.data;
    for (var p = 0, q = 0; p < hitData.length; p += 4, q += 4) {
      if (hitData[p] === id) {
        d[q] = 31; d[q + 1] = 111; d[q + 2] = 235; d[q + 3] = 64;
      }
    }
    cc.putImageData(img, 0, 0);
    cache[id] = c;
    return c;
  }

  function highlight(id) {
    if (id === active) return;
    active = id;
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    document.querySelectorAll(".flat").forEach(function (el) { el.classList.remove("hl"); });
    if (!id) { hint.style.display = "none"; return; }
    ctx.drawImage(silhouette(id), 0, 0);
    var el = document.querySelector('.flat[data-idx="' + id + '"]');
    if (el) el.classList.add("hl");
  }

  plan.addEventListener("mousemove", function (e) {
    if (!hitData) return;
    var r = plan.getBoundingClientRect();
    var x = (e.clientX - r.left) * hit.width / r.width;
    var y = (e.clientY - r.top) * hit.height / r.height;
    var id = idAt(x, y);
    highlight(id);
    var flat = flats[id];
    if (flat) {
      hint.textContent = flat.label + " — нажмите, чтобы скачать";
      hint.style.left = (e.clientX - r.left) + "px";
      hint.style.top = (e.clientY - r.top) + "px";
      hint.style.display = "block";
    } else {
      hint.style.display = "none";
    }
  });

  plan.addEventListener("mouseleave", function () { highlight(0); });

  plan.addEventListener("click", function () {
    var flat = flats[active];
    if (!flat) return;
    window.location = "/files/floors/" + floorId + "/apartments/" +
      encodeURIComponent(flat.filename) + "?download=1";
  });

  function renderList(list) {
    flats = {};
    list.forEach(function (a) { flats[a.idx] = a; });
    var box = document.getElementById("flat-list");
    if (!box) return;
    box.innerHTML = "";
    list.forEach(function (a) {
      var el = document.createElement("div");
      el.className = "flat";
      el.dataset.idx = a.idx;
      el.innerHTML = '<b>' + a.label + '</b><span class="spacer"></span>' +
        '<a class="btn" href="/files/floors/' + floorId + '/apartments/' +
        encodeURIComponent(a.filename) + '?download=1">Скачать</a>';
      el.addEventListener("mouseenter", function () { highlight(a.idx); });
      el.addEventListener("mouseleave", function () { highlight(0); });
      box.appendChild(el);
    });
  }

  var statusEl = document.getElementById("floor-status");
  var busy = root.dataset.status === "queued" || root.dataset.status === "working";

  function poll() {
    fetch("/api/floors/" + floorId).then(function (r) { return r.json(); }).then(function (d) {
      if (statusEl) {
        statusEl.className = "status " + d.status;
        statusEl.textContent = d.message || d.status;
      }
      if (d.status === "queued" || d.status === "working") {
        setTimeout(poll, 2000);
      } else {
        window.location.reload();
      }
    }).catch(function () { setTimeout(poll, 4000); });
  }

  if (busy) { setTimeout(poll, 1500); }
  if (root.dataset.status === "done") {
    loadHitmap();
    try { renderList(JSON.parse(root.dataset.flats || "[]")); } catch (e) {}
  }
})();
