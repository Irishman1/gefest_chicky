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
      if (editing || !hitData) return;
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

    plan.addEventListener("mouseleave", function () {
      if (editing) return;
      highlight(0);
    });

    plan.addEventListener("click", function () {
      if (editing) return;
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

      el.append(name, gap, link, renameBtn(a), deleteBtn(a));
      el.addEventListener("mouseenter", function () { highlight(a.idx); });
      el.addEventListener("mouseleave", function () { highlight(0); });
      box.appendChild(el);
    });
  }

  // ---------------------------------------------------------------- правка
  var projectId = root.dataset.project;
  var floorNo = root.dataset.floorNumber;
  var editing = false, points = [];

  function editUrl(what) {
    return "/projects/" + projectId + "/floors/" + floorNo + "/edits/" + what;
  }

  function post(url, fields) {
    var f = document.createElement("form");
    f.method = "post";
    f.action = url;
    Object.keys(fields).forEach(function (k) {
      var i = document.createElement("input");
      i.type = "hidden"; i.name = k; i.value = fields[k];
      f.appendChild(i);
    });
    document.body.appendChild(f);
    f.submit();
  }

  function iconBtn(title, path, cls) {
    var b = document.createElement("button");
    b.type = "button";
    b.className = "btn-sm ghost icon-only " + (cls || "");
    b.title = title;
    b.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" ' +
      'stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
      'stroke-linejoin="round">' + path + "</svg>";
    return b;
  }

  function renameBtn(a) {
    var b = iconBtn("Переименовать",
      '<path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>');
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      var v = window.prompt("Новый номер для " + a.label, a.number);
      if (v === null) return;
      v = v.trim();
      if (!v || v === a.number) return;
      post(editUrl("rename"), { target: a.number, flat: v });
    });
    return b;
  }

  function deleteBtn(a) {
    var b = iconBtn("Удалить",
      '<path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2M6 7l1 13h10l1-13"/>',
      "danger");
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      if (!window.confirm("Удалить " + a.label + " из нарезки?")) return;
      post(editUrl("delete"), { target: a.number });
    });
    return b;
  }

  var toggle = document.getElementById("edit-toggle");
  var editHint = document.getElementById("edit-hint");
  var addForm = document.getElementById("edit-add");
  var polyField = document.getElementById("edit-polygon");
  var cancelBtn = document.getElementById("edit-cancel");

  function drawPoints() {
    if (!ctx) return;
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    if (!points.length) return;
    var s = overlay.width;                       // контур храним в долях 0..1
    ctx.save();
    ctx.beginPath();
    points.forEach(function (p, i) {
      var x = p[0] * overlay.width, y = p[1] * overlay.height;
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    if (points.length > 2) ctx.closePath();
    ctx.fillStyle = "rgba(31,111,235,.18)";
    ctx.strokeStyle = "rgb(31,111,235)";
    ctx.lineWidth = Math.max(s / 400, 2);
    if (points.length > 2) ctx.fill();
    ctx.stroke();
    points.forEach(function (p) {
      ctx.beginPath();
      ctx.arc(p[0] * overlay.width, p[1] * overlay.height,
              Math.max(s / 250, 4), 0, Math.PI * 2);
      ctx.fillStyle = "rgb(31,111,235)";
      ctx.fill();
    });
    ctx.restore();
  }

  function setEditing(on) {
    editing = on;
    points = [];
    active = 0;
    if (ctx) ctx.clearRect(0, 0, overlay.width, overlay.height);
    if (editHint) editHint.hidden = !on;
    if (addForm) addForm.hidden = true;
    if (toggle) {
      toggle.classList.toggle("primary", on);
      toggle.lastChild.nodeValue = on ? " Готово" : " Править вручную";
    }
    if (plan) plan.style.cursor = on ? "copy" : "crosshair";
    if (hint) hint.style.display = "none";
  }


  if (toggle && plan && overlay) {
    toggle.addEventListener("click", function () { setEditing(!editing); });

    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () { setEditing(true); });
    }

    plan.addEventListener("click", function (e) {
      if (!editing) return;
      var r = plan.getBoundingClientRect();
      points.push([(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height]);
      drawPoints();
      if (addForm) addForm.hidden = points.length < 3;
      if (polyField) polyField.value = JSON.stringify(points);
    });

    plan.addEventListener("dblclick", function (e) {
      if (!editing || points.length < 3) return;
      e.preventDefault();
      if (addForm) {
        addForm.hidden = false;
        var f = document.getElementById("edit-flat");
        if (f) f.focus();
      }
    });

    document.addEventListener("keydown", function (e) {
      if (!editing) return;
      if (e.key === "Escape") setEditing(true);
      if (e.key === "Backspace" && points.length) {
        e.preventDefault();
        points.pop();
        drawPoints();
        if (addForm) addForm.hidden = points.length < 3;
        if (polyField) polyField.value = JSON.stringify(points);
      }
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
