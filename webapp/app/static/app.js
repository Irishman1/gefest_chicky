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

  function paint() {
    if (!ctx) return;
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    if (active && hitData) ctx.drawImage(silhouette(active), 0, 0);
  }

  function highlight(id) {
    if (id === active) return;
    active = id;
    document.querySelectorAll(".flat").forEach(function (el) { el.classList.remove("hl"); });
    if (!id) { hint.style.display = "none"; paint(); return; }
    paint();
    var el = document.querySelector('.flat[data-idx="' + id + '"]');
    if (el) el.classList.add("hl");
  }

  // при смене темы перерисовываем подсветку новым акцентом
  document.addEventListener("themechange", function () {
    cache = {};
    if (hitData) paint();
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
        hint.textContent = flat.label;
        hint.style.left = (e.clientX - r.left) + "px";
        hint.style.top = (e.clientY - r.top) + "px";
        hint.style.display = "block";
      } else {
        hint.style.display = "none";
      }
    });

    plan.addEventListener("mouseleave", function () { highlight(0); });

    plan.addEventListener("click", function (e) {
      var flat = flats[active];
      if (!flat) { closePick(); return; }
      var r = plan.getBoundingClientRect();
      showPick(flat, e.clientX - r.left, e.clientY - r.top);
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

      // миниатюра самой вырезки — видно, что получилось, не открывая файл
      var shot = document.createElement("a");
      shot.className = "flat-shot";
      shot.href = "/files/floors/" + floorId + "/apartments/" +
        encodeURIComponent(a.filename);
      shot.target = "_blank";
      shot.title = "Открыть вырезку целиком";
      var img = document.createElement("img");
      img.loading = "lazy";
      img.alt = a.label;
      img.src = "/files/floors/" + floorId + "/thumbs/" +
        encodeURIComponent(a.filename);
      shot.appendChild(img);

      var head = document.createElement("div");
      head.className = "flat-head";
      var name = document.createElement("b");
      name.textContent = a.label;
      var gap = document.createElement("span");
      gap.className = "spacer";
      head.append(name, gap, renameBtn(a), redrawBtn(a), deleteBtn(a));

      var link = document.createElement("a");
      link.className = "btn btn-sm btn-block";
      link.href = "/files/floors/" + floorId + "/apartments/" +
        encodeURIComponent(a.filename) + "?download=1";
      link.textContent = "Скачать";

      el.append(shot, head, link);
      el.addEventListener("mouseenter", function () { highlight(a.idx); });
      el.addEventListener("mouseleave", function () { highlight(0); });
      box.appendChild(el);
    });
  }

  // ---------------------------------------------------------------- правка
  var projectId = root.dataset.project;
  var floorNo = root.dataset.floorNumber;

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

  function redrawBtn(a) {
    var b = iconBtn("Поправить контур",
      '<path d="M3 12a9 9 0 1 0 3-6.7L3 8"/><path d="M3 3v5h5"/>');
    b.addEventListener("click", function (ev) {
      ev.stopPropagation();
      openEditor(a);
    });
    return b;
  }

  // ---- контур существующей вырезки: обводим маску, чтобы её можно было
  //      править за точки, а не рисовать заново

  function traceContour(id) {
    if (!hitData || !hit) return null;
    var W = hit.width, H = hit.height;
    function on(x, y) {
      return x >= 0 && y >= 0 && x < W && y < H && hitData[(y * W + x) * 4] === id;
    }
    var sx = -1, sy = -1;
    for (var y = 0; y < H && sy < 0; y++) {
      for (var x = 0; x < W; x++) { if (on(x, y)) { sx = x; sy = y; break; } }
    }
    if (sy < 0) return null;

    // марширующие квадраты: идём по границе между закрашенным и пустым
    var pts = [], cx = sx, cy = sy, dx = 0, dy = 0, steps = 0, limit = 4 * (W + H) * 4;
    do {
      var st = (on(cx - 1, cy - 1) ? 1 : 0) | (on(cx, cy - 1) ? 2 : 0)
             | (on(cx - 1, cy) ? 4 : 0) | (on(cx, cy) ? 8 : 0);
      var nx = 0, ny = 0;
      if (st === 1 || st === 5 || st === 13) { ny = -1; }
      else if (st === 2 || st === 3 || st === 7) { nx = 1; }
      else if (st === 4 || st === 12 || st === 14) { nx = -1; }
      else if (st === 8 || st === 10 || st === 11) { ny = 1; }
      else if (st === 6) { nx = (dy === -1) ? -1 : 1; }
      else if (st === 9) { ny = (dx === 1) ? -1 : 1; }
      else break;
      if (nx !== dx || ny !== dy) pts.push([cx, cy]);     // угол
      dx = nx; dy = ny; cx += dx; cy += dy;
      if (++steps > limit) break;
    } while (!(cx === sx && cy === sy));
    return pts.length >= 3 ? pts : null;
  }

  // Рамер—Дуглас—Пойкер: из тысяч ступенек границы делаем горсть вершин,
  // за которые реально можно ухватиться мышью
  function simplify(pts, eps) {
    if (pts.length < 3) return pts;
    function seg(a, b, p) {
      var vx = b[0] - a[0], vy = b[1] - a[1];
      var len = Math.hypot(vx, vy) || 1;
      return Math.abs((p[0] - a[0]) * vy - (p[1] - a[1]) * vx) / len;
    }
    function walk(first, last) {
      var worst = 0, idx = -1;
      for (var i = first + 1; i < last; i++) {
        var d = seg(pts[first], pts[last], pts[i]);
        if (d > worst) { worst = d; idx = i; }
      }
      if (worst <= eps || idx < 0) return [pts[first]];
      return walk(first, idx).concat(walk(idx, last));
    }
    return walk(0, pts.length - 1).concat([pts[pts.length - 1]]);
  }

  // Порог упрощения держим мелким: контур для правки должен повторять
  // автоматическую границу, иначе «поправить» означало бы «огрубить».
  var MAX_POINTS = 260;

  function contourOf(id) {
    var raw = traceContour(id);
    if (!raw) return null;
    var eps = Math.max(hit.width, hit.height) / 700;
    var out = simplify(raw, eps);
    for (var i = 0; i < 8 && out.length > MAX_POINTS; i++) {
      eps *= 1.5;
      out = simplify(raw, eps);
    }
    return out.map(function (p) { return [p[0] / hit.width, p[1] / hit.height]; });
  }

  // ---- всплывающий выбор: редактировать или скачать
  var pick = document.getElementById("pick");
  var pickLabel = document.getElementById("pick-label");
  var pickEdit = document.getElementById("pick-edit");
  var pickDl = document.getElementById("pick-download");
  var pickClose = document.getElementById("pick-close");
  var picked = null;

  function closePick() {
    picked = null;
    if (pick) pick.hidden = true;
  }

  function showPick(flat, x, y) {
    if (!pick) return;
    picked = flat;
    pickLabel.textContent = flat.label;
    pickDl.href = "/files/floors/" + floorId + "/apartments/" +
      encodeURIComponent(flat.filename) + "?download=1";
    pick.style.left = x + "px";
    pick.style.top = y + "px";
    pick.hidden = false;
    hint.style.display = "none";
  }

  if (pickClose) pickClose.addEventListener("click", closePick);
  if (pickEdit) {
    pickEdit.addEventListener("click", function () {
      if (picked) openEditor(picked);
    });
  }

  // ------------------------------------------------- окно правки контура
  // Правку вынесли в отдельное окно: на общем плане квартира размером с марку,
  // и попасть мышью в вершину было нельзя. Здесь она во весь экран.
  var ed = {
    box: document.getElementById("editor"),
    view: document.getElementById("editor-view"),
    plan: document.getElementById("editor-plan"),
    canvas: document.getElementById("editor-canvas"),
    title: document.getElementById("editor-title"),
    hint: document.getElementById("editor-hint"),
    count: document.getElementById("editor-count"),
    form: document.getElementById("editor-form"),
    poly: document.getElementById("editor-polygon"),
    target: document.getElementById("editor-target"),
    flat: document.getElementById("editor-flat"),
    drop: document.getElementById("editor-drop"),
    zoomLevel: document.getElementById("editor-zoom"),
    save: document.getElementById("editor-save")
  };
  var ectx = ed.canvas ? ed.canvas.getContext("2d") : null;

  var points = [];                 // вершины контура в долях 0..1
  var shapeMode = false;           // правим готовый контур, а не рисуем новый
  var dragIdx = -1, hoverIdx = -1, selIdx = -1;
  var ezoom = 1, efit = 0;

  var DRAW_HINT = "Кликайте по плану — обводите помещение. Минимум три точки, " +
    "двойной клик замыкает контур.";
  var SHAPE_HINT = "Тяните вершины мышью. Клик по линии добавит вершину, " +
    "Alt+клик или Delete уберёт ту, что под курсором.";

  function ready() {
    return ed.box && ectx;
  }

  function syncPoly() {
    if (ed.poly) ed.poly.value = JSON.stringify(points);
    if (ed.count) {
      ed.count.textContent = points.length ? "точек: " + points.length : "";
    }
    if (ed.save) ed.save.disabled = points.length < 3;
    if (ed.drop) {
      ed.drop.disabled = !points.length || (shapeMode && points.length <= 3);
    }
  }

  function drawEditor() {
    if (!ectx) return;
    var W = ed.canvas.width, H = ed.canvas.height;
    ectx.clearRect(0, 0, W, H);
    if (!points.length) return;
    // Холст растянут на план, поэтому в пикселях картинки вершины росли бы
    // вместе с приближением. Считаем их в экранных пикселях: k — сколько
    // пикселей холста приходится на один экранный.
    var k = W / (ed.plan.clientWidth || W);
    ectx.save();
    ectx.beginPath();
    points.forEach(function (p, i) {
      var x = p[0] * W, y = p[1] * H;
      if (i === 0) ectx.moveTo(x, y); else ectx.lineTo(x, y);
    });
    if (points.length > 2) ectx.closePath();
    ectx.fillStyle = "rgba(31,111,235,.14)";
    ectx.strokeStyle = "rgb(31,111,235)";
    ectx.lineWidth = 1.5 * k;
    if (points.length > 2) ectx.fill();
    ectx.stroke();
    points.forEach(function (p, i) {
      var live = (i === dragIdx || i === hoverIdx || i === selIdx);
      ectx.beginPath();
      ectx.arc(p[0] * W, p[1] * H, (live ? 5.5 : 3.2) * k, 0, Math.PI * 2);
      // вершина под курсором крупнее — видно, за что берёшься;
      // первая белая — по ней видно, где замкнётся контур
      ectx.fillStyle = i === selIdx ? "rgb(180,35,24)"
        : (live || (i === 0 && !shapeMode) ? "#fff" : "rgb(31,111,235)");
      ectx.fill();
      ectx.lineWidth = (live ? 2 : 1.5) * k;
      ectx.stroke();
    });
    ectx.restore();
  }

  function applyEZoom(next, ax, ay) {
    if (!efit) return;
    next = Math.min(Math.max(next, 1), 12);
    var r = ed.view.getBoundingClientRect();
    if (ax === undefined) ax = r.width / 2;
    if (ay === undefined) ay = r.height / 2;
    var px = (ed.view.scrollLeft + ax) / ezoom;
    var py = (ed.view.scrollTop + ay) / ezoom;
    ezoom = next;
    ed.plan.style.maxWidth = "none";
    ed.plan.style.width = Math.round(efit * ezoom) + "px";
    ed.view.scrollLeft = px * ezoom - ax;
    ed.view.scrollTop = py * ezoom - ay;
    if (ed.zoomLevel) ed.zoomLevel.textContent = Math.round(ezoom * 100) + "%";
    drawEditor();
  }

  function fitEditor(flat) {
    ed.plan.style.width = "";
    ed.plan.style.maxWidth = "100%";
    ezoom = 1;
    efit = ed.plan.clientWidth;
    if (ed.zoomLevel) ed.zoomLevel.textContent = "100%";
    if (!flat || !flat.box || !ed.canvas.width) { drawEditor(); return; }
    // подводим окно к самой квартире: соседей видно, но правим свою
    var box = flat.box;
    var fw = Math.max((box[2] - box[0]) / ed.canvas.width, 0.002);
    var fh = Math.max((box[3] - box[1]) / ed.canvas.height, 0.002);
    var planH = efit * ed.canvas.height / ed.canvas.width;
    var r = ed.view.getBoundingClientRect();
    applyEZoom(Math.min(r.width / (fw * efit), r.height / (fh * planH)) * 0.82);
    ed.view.scrollLeft = (box[0] + box[2]) / 2 / ed.canvas.width * ed.plan.clientWidth - r.width / 2;
    ed.view.scrollTop = (box[1] + box[3]) / 2 / ed.canvas.height * ed.plan.clientHeight - r.height / 2;
    drawEditor();
  }

  function openEditor(flat) {
    if (!ready()) return;
    closePick();
    highlight(0);
    points = [];
    dragIdx = hoverIdx = selIdx = -1;
    shapeMode = false;

    // Карта попаданий и план сняты в одном разрешении; пока она не загрузилась,
    // обводить с нуля всё равно можно — берём размер самой картинки плана.
    ed.canvas.width = hit ? hit.width : (plan.naturalWidth || plan.clientWidth);
    ed.canvas.height = hit ? hit.height : (plan.naturalHeight || plan.clientHeight);

    if (flat) {
      // Обводим уже вырезанное: тянуть вершины быстрее, чем обводить заново.
      // Если контур снять не удалось — остаётся обычная обводка с нуля.
      var got = hitData ? contourOf(flat.idx) : null;
      if (got && got.length >= 3) { points = got; shapeMode = true; }
      ed.title.textContent = "Контур " + flat.label;
      ed.flat.value = flat.number;
      ed.target.value = flat.number;
      ed.form.action = editUrl("replace");
    } else {
      ed.title.textContent = "Новая планировка";
      ed.flat.value = "";
      ed.target.value = "";
      ed.form.action = editUrl("add");
    }
    ed.hint.textContent = shapeMode ? SHAPE_HINT : DRAW_HINT;
    syncPoly();

    ed.box.hidden = false;
    document.body.classList.add("modal-open");
    if (!ed.plan.getAttribute("src")) {
      ed.plan.src = plan.getAttribute("src");
    }
    if (ed.plan.complete) fitEditor(flat);
    else ed.plan.addEventListener("load", function once() {
      ed.plan.removeEventListener("load", once);
      fitEditor(flat);
    });
  }

  function closeEditor() {
    if (!ed.box) return;
    ed.box.hidden = true;
    document.body.classList.remove("modal-open");
    points = [];
    dragIdx = hoverIdx = selIdx = -1;
  }

  function localPt(e) {
    var r = ed.plan.getBoundingClientRect();
    return [(e.clientX - r.left) / r.width, (e.clientY - r.top) / r.height, r];
  }

  // ближайшая вершина в пределах grab экранных пикселей
  function vertexAt(fx, fy, r, grab) {
    var best = -1, bestD = grab;
    points.forEach(function (p, i) {
      var d = Math.hypot((p[0] - fx) * r.width, (p[1] - fy) * r.height);
      if (d < bestD) { bestD = d; best = i; }
    });
    return best;
  }

  // ребро, на которое пришёлся клик — туда вставим новую вершину
  function edgeAt(fx, fy, r, grab) {
    if (points.length < 2) return -1;
    var best = -1, bestD = grab;
    for (var i = 0; i < points.length; i++) {
      // Пока обводят с нуля, клик рядом с последними рёбрами — это продолжение
      // контура, а не желание разрезать только что поставленное ребро.
      if (!shapeMode && i >= points.length - 2) continue;
      var a = points[i], b = points[(i + 1) % points.length];
      var ax = a[0] * r.width, ay = a[1] * r.height;
      var bx = b[0] * r.width, by = b[1] * r.height;
      var px = fx * r.width, py = fy * r.height;
      var vx = bx - ax, vy = by - ay;
      var len2 = vx * vx + vy * vy;
      var t = len2 ? Math.max(0, Math.min(1, ((px - ax) * vx + (py - ay) * vy) / len2)) : 0;
      var d = Math.hypot(px - (ax + t * vx), py - (ay + t * vy));
      if (d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  // вершина, с которой работают кнопка и Delete: под курсором, иначе выбранная,
  // иначе последняя поставленная
  function current() {
    if (hoverIdx >= 0 && hoverIdx < points.length) return hoverIdx;
    if (selIdx >= 0 && selIdx < points.length) return selIdx;
    return points.length - 1;
  }

  function dropPoint(i) {
    if (i < 0 || i >= points.length) return;
    if (shapeMode && points.length <= 3) return;   // иначе контур перестанет быть контуром
    points.splice(i, 1);
    if (selIdx === i) selIdx = -1;
    else if (selIdx > i) selIdx--;
    if (hoverIdx >= points.length) hoverIdx = -1;
    syncPoly();
    drawEditor();
  }

  if (ed.box) {
    ed.plan.addEventListener("mousedown", function (e) {
      if (e.button !== 0) return;
      var lp = localPt(e), fx = lp[0], fy = lp[1], r = lp[2];
      var v = vertexAt(fx, fy, r, 12);
      if (v >= 0) {
        if (e.altKey) { dropPoint(v); e.preventDefault(); return; }
        selIdx = v;
        dragIdx = v;
        syncPoly();
        drawEditor();
        e.preventDefault();
        return;
      }
      var edge = edgeAt(fx, fy, r, 10);
      if (edge >= 0) {                            // клик по линии — новая вершина
        points.splice(edge + 1, 0, [fx, fy]);
        dragIdx = selIdx = edge + 1;
        syncPoly();
        drawEditor();
        e.preventDefault();
        return;
      }
      if (!shapeMode) {                           // обводка с нуля
        points.push([fx, fy]);
        selIdx = points.length - 1;
        syncPoly();
        drawEditor();
        e.preventDefault();
      }
    });

    ed.plan.addEventListener("mousemove", function (e) {
      var lp = localPt(e), fx = lp[0], fy = lp[1], r = lp[2];
      if (dragIdx >= 0) {
        points[dragIdx] = [Math.min(Math.max(fx, 0), 1), Math.min(Math.max(fy, 0), 1)];
        syncPoly();
        drawEditor();
        return;
      }
      var v = vertexAt(fx, fy, r, 12);
      if (v !== hoverIdx) { hoverIdx = v; drawEditor(); }
      ed.plan.style.cursor = v >= 0 ? "grab"
        : (edgeAt(fx, fy, r, 10) >= 0 ? "copy" : (shapeMode ? "default" : "crosshair"));
    });

    document.addEventListener("mouseup", function () {
      if (dragIdx >= 0) { dragIdx = -1; syncPoly(); drawEditor(); }
    });

    ed.plan.addEventListener("dblclick", function (e) {
      if (shapeMode || points.length < 3) return;
      e.preventDefault();
      if (ed.flat) ed.flat.focus();
    });

    // масштаб и сдвиг внутри окна правки
    ed.view.addEventListener("wheel", function (e) {
      if (!efit) return;
      e.preventDefault();
      var r = ed.view.getBoundingClientRect();
      applyEZoom(ezoom * (e.deltaY < 0 ? 1.2 : 1 / 1.2),
                 e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });

    ed.box.querySelectorAll("[data-ezoom]").forEach(function (b) {
      b.addEventListener("click", function () {
        var what = b.dataset.ezoom;
        if (what === "in") applyEZoom(ezoom * 1.4);
        else if (what === "out") applyEZoom(ezoom / 1.4);
        else fitEditor(null);
      });
    });

    var epan = null;
    ed.view.addEventListener("contextmenu", function (e) { e.preventDefault(); });
    ed.view.addEventListener("mousedown", function (e) {
      if (e.button !== 2 && e.button !== 1) return;
      e.preventDefault();
      epan = { x: e.clientX, y: e.clientY, l: ed.view.scrollLeft, t: ed.view.scrollTop };
      ed.view.classList.add("panning");
    });
    document.addEventListener("mousemove", function (e) {
      if (!epan) return;
      ed.view.scrollLeft = epan.l - (e.clientX - epan.x);
      ed.view.scrollTop = epan.t - (e.clientY - epan.y);
    });
    document.addEventListener("mouseup", function () {
      if (!epan) return;
      epan = null;
      ed.view.classList.remove("panning");
    });

    if (ed.drop) {
      ed.drop.addEventListener("click", function () { dropPoint(current()); });
    }

    ed.box.querySelectorAll("[data-editor-close]").forEach(function (b) {
      b.addEventListener("click", closeEditor);
    });
    ed.box.addEventListener("mousedown", function (e) {
      if (e.target === ed.box) closeEditor();     // клик по затемнению
    });

    ed.form.addEventListener("submit", function (e) {
      if (points.length < 3) {
        e.preventDefault();
        return;
      }
      syncPoly();
    });

    document.addEventListener("keydown", function (e) {
      if (ed.box.hidden) return;
      if (e.key === "Escape") { closeEditor(); return; }
      if (document.activeElement === ed.flat) return;
      if (e.key === "Delete" || e.key === "Backspace") {
        e.preventDefault();
        dropPoint(current());
      }
    });
  }

  var newBtn = document.getElementById("edit-new");
  if (newBtn) {
    newBtn.addEventListener("click", function () { openEditor(null); });
  }

  // ---------------------------------------------------------------- масштаб
  // План растягиваем шириной самой картинки, а контейнер прокручиваем. Все
  // координаты и наведение, и обводка считают через getBoundingClientRect,
  // поэтому от масштаба не зависят.
  var zoom = 1, fitWidth = 0;
  var levelEl = document.getElementById("zoom-level");

  function applyZoom(next, anchorX, anchorY) {
    if (!plan || !fitWidth) return;
    next = Math.min(Math.max(next, 1), 8);
    if (next === zoom) return;

    var r = root.getBoundingClientRect();
    var ax = anchorX === undefined ? r.width / 2 : anchorX;
    var ay = anchorY === undefined ? r.height / 2 : anchorY;
    var px = (root.scrollLeft + ax) / zoom;        // точка под курсором
    var py = (root.scrollTop + ay) / zoom;

    zoom = next;
    plan.style.maxWidth = "none";
    plan.style.width = Math.round(fitWidth * zoom) + "px";

    root.scrollLeft = px * zoom - ax;
    root.scrollTop = py * zoom - ay;
    root.classList.toggle("zoomed", zoom > 1);
    if (levelEl) levelEl.textContent = Math.round(zoom * 100) + "%";
    paint();
  }

  function initZoom() {
    if (!plan) return;
    fitWidth = plan.clientWidth;
    if (levelEl) levelEl.textContent = "100%";
  }

  if (plan) {
    if (plan.complete) initZoom();
    else plan.addEventListener("load", initZoom);
  }

  document.querySelectorAll("[data-zoom]").forEach(function (b) {
    b.addEventListener("click", function () {
      var what = b.dataset.zoom;
      if (what === "in") applyZoom(zoom * 1.4);
      else if (what === "out") applyZoom(zoom / 1.4);
      else {
        zoom = 1.0001;                             // чтобы applyZoom не отбросил
        applyZoom(1);
        plan.style.width = "";
        plan.style.maxWidth = "";
        root.scrollLeft = root.scrollTop = 0;
        root.classList.remove("zoomed");
        if (levelEl) levelEl.textContent = "100%";
      }
    });
  });

  root.addEventListener("wheel", function (e) {
    if (!e.ctrlKey || !fitWidth) return;
    e.preventDefault();
    var r = root.getBoundingClientRect();
    applyZoom(zoom * (e.deltaY < 0 ? 1.15 : 1 / 1.15),
              e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });

  // сдвиг правой кнопкой — левая занята выбором квартиры
  var pan = null;
  root.addEventListener("contextmenu", function (e) {
    if (zoom > 1) e.preventDefault();
  });
  root.addEventListener("mousedown", function (e) {
    if (e.button !== 2 || zoom <= 1) return;
    e.preventDefault();
    pan = { x: e.clientX, y: e.clientY, l: root.scrollLeft, t: root.scrollTop };
    root.classList.add("panning");
  });
  document.addEventListener("mousemove", function (e) {
    if (!pan) return;
    root.scrollLeft = pan.l - (e.clientX - pan.x);
    root.scrollTop = pan.t - (e.clientY - pan.y);
  });
  document.addEventListener("mouseup", function () {
    pan = null;
    root.classList.remove("panning");
  });

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
