// Тема
(function () {
  var btn = document.getElementById("theme-toggle");
  if (!btn) return;
  btn.addEventListener("click", function () {
    var next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("theme", next); } catch (e) {}
    document.dispatchEvent(new CustomEvent("themechange"));
  });
})();

// Копирование ссылки-приглашения
(function () {
  document.querySelectorAll(".copy").forEach(function (btn) {
    var label = btn.querySelector("span");
    btn.addEventListener("click", function () {
      var url = new URL(btn.dataset.copy, location.origin).href;
      navigator.clipboard.writeText(url).then(function () {
        var was = label.textContent;
        label.textContent = "скопировано";
        setTimeout(function () { label.textContent = was; }, 1200);
      }).catch(function () {});
    });
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
  var ctx = overlay ? overlay.getContext("2d") : null;
  var hit = null, hitCtx = null, hitData = null;
  var flats = {}, cache = {}, active = 0;

  // цвет подсветки берём из палитры темы, чтобы план и интерфейс совпадали
  function hlColor() {
    var raw = getComputedStyle(document.documentElement).getPropertyValue("--hl-rgb");
    var p = raw.trim().split(/[\s,]+/).map(Number);
    return p.length === 3 && p.every(function (n) { return n >= 0 && n <= 255; }) ? p : [31, 111, 235];
  }

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
    var rgb = hlColor();
    var c = document.createElement("canvas");
    c.width = hit.width; c.height = hit.height;
    var cc = c.getContext("2d");
    var img = cc.createImageData(hit.width, hit.height);
    var d = img.data;
    for (var p = 0, q = 0; p < hitData.length; p += 4, q += 4) {
      if (hitData[p] === id) {
        d[q] = rgb[0]; d[q + 1] = rgb[1]; d[q + 2] = rgb[2]; d[q + 3] = 66;
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

  // при смене темы перерисовываем подсветку новым акцентом
  document.addEventListener("themechange", function () {
    cache = {};
    if (!hitData || !active) return;
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    ctx.drawImage(silhouette(active), 0, 0);
  });

  if (plan) {
    plan.addEventListener("mousemove", function (e) {
      if (!hitData) return;
      var r = plan.getBoundingClientRect();
      var x = (e.clientX - r.left) * hit.width / r.width;
      var y = (e.clientY - r.top) * hit.height / r.height;
      highlight(idAt(x, y));
      var flat = flats[active];
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
  }

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

      var name = document.createElement("b");
      name.textContent = a.label;

      var gap = document.createElement("span");
      gap.className = "spacer";

      var link = document.createElement("a");
      link.className = "btn btn-sm";
      link.href = "/files/floors/" + floorId + "/apartments/" +
        encodeURIComponent(a.filename) + "?download=1";
      link.textContent = "Скачать";

      el.append(name, gap, link);
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
