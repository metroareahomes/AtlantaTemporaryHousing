/* Keep visual column labels and native Save actions in view. No automation requests. */
(function () {
  "use strict";
  var main = document.querySelector("main");
  if (!main) return;
  var saveForms = Array.from(main.querySelectorAll("form[data-save-label]"));
  if (saveForms.length) {
    var bar = document.createElement("div");
    bar.className = "save-bar";
    bar.setAttribute("role", "region");
    bar.setAttribute("aria-label", "Save changes");
    var hint = document.createElement("span");
    hint.className = "save-bar-hint";
    hint.textContent = "Changes are saved only when you press Save.";
    bar.appendChild(hint);
    saveForms.forEach(function (form) {
      var button = document.createElement("button");
      button.type = "submit";
      button.className = "primary";
      button.setAttribute("form", form.id);
      button.textContent = form.dataset.saveLabel;
      var state = document.createElement("span");
      state.className = "save-state";
      state.setAttribute("role", "status");
      var group = document.createElement("div");
      group.className = "save-action";
      group.append(button, state);
      bar.appendChild(group);
      function changed(event) {
        // Search filters are not saved settings.
        if (!event.target.name || event.target.type === "search") return;
        state.textContent = "Unsaved changes";
        button.classList.add("has-changes");
      }
      form.addEventListener("input", changed);
      form.addEventListener("change", changed);
    });
    document.body.appendChild(bar);
    function sizeBar() {
      document.documentElement.style.setProperty("--save-bar-height", bar.getBoundingClientRect().height + "px");
    }
    sizeBar();
    if (window.ResizeObserver) new ResizeObserver(sizeBar).observe(bar);
    window.addEventListener("resize", sizeBar);
  }

  var floating = document.createElement("div");
  floating.className = "floating-columns";
  floating.setAttribute("aria-hidden", "true");
  floating.setAttribute("inert", "");
  floating.hidden = true;
  document.body.appendChild(floating);
  var pending = false;
  var appHeader = document.querySelector("body > header");
  function sizeHeader() {
    document.documentElement.style.setProperty("--app-header-height",
      (appHeader ? appHeader.getBoundingClientRect().height : 0) + "px");
  }
  function update() {
    pending = false;
    var top = appHeader ? Math.max(0, appHeader.getBoundingClientRect().bottom) : 0;
    var headers = Array.from(main.querySelectorAll(".scroll > table > thead, .homes > .h.head"));
    var chosen = null;
    headers.forEach(function (head) {
      var rect = head.getBoundingClientRect();
      var container = head.closest(".scroll, .homes");
      var bounds = container.getBoundingClientRect();
      if (rect.width && rect.height && rect.top < top && bounds.bottom > top
          && bounds.right > 0 && bounds.left < window.innerWidth) {
        chosen = {head: head, rect: rect, container: container, bounds: bounds};
      }
    });
    floating.replaceChildren();
    floating.hidden = !chosen;
    if (!chosen) return;
    var head = chosen.head, rect = chosen.rect, bounds = chosen.bounds;
    var left = Math.max(0, bounds.left);
    var right = Math.min(window.innerWidth, bounds.right);
    floating.style.left = left + "px";
    floating.style.width = Math.max(0, right - left) + "px";
    floating.style.top = Math.min(top, bounds.bottom - rect.height) + "px";
    var copy;
    if (head.tagName === "THEAD") {
      var table = head.closest("table");
      copy = document.createElement("table");
      copy.className = table.className;
      copy.style.tableLayout = "fixed";
      copy.style.width = table.getBoundingClientRect().width + "px";
      var cols = document.createElement("colgroup");
      Array.from(head.rows[0].cells).forEach(function (cell) {
        var col = document.createElement("col");
        col.style.width = cell.getBoundingClientRect().width + "px";
        cols.appendChild(col);
      });
      copy.append(cols, head.cloneNode(true));
    } else {
      copy = head.cloneNode(true);
      copy.style.width = rect.width + "px";
      copy.style.gridTemplateColumns = getComputedStyle(head).gridTemplateColumns;
    }
    copy.style.marginLeft = (rect.left - left) + "px";
    // Copies are visual only. Keep document IDs unique.
    copy.removeAttribute("id");
    copy.querySelectorAll("[id]").forEach(function (el) { el.removeAttribute("id"); });
    floating.appendChild(copy);
  }
  function schedule() {
    if (!pending) { pending = true; requestAnimationFrame(update); }
  }
  // Capture catches horizontal scrolling inside tables as well as page scrolling.
  document.addEventListener("scroll", schedule, {capture: true, passive: true});
  window.addEventListener("resize", function () { sizeHeader(); schedule(); });
  sizeHeader();
  if (appHeader && window.ResizeObserver) {
    new ResizeObserver(function () { sizeHeader(); schedule(); }).observe(appHeader);
  }
  if (window.ResizeObserver) new ResizeObserver(schedule).observe(main);
  new MutationObserver(schedule).observe(main, {childList: true, subtree: true, attributes: true});
  schedule();
})();
