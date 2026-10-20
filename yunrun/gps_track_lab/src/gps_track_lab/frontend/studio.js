(function () {
  "use strict";
  var paths = {
    overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
    run:'<path d="m13 4 3 1-2 5 4 3 3-1M6 20l5-7-3-3-4 3m4-3 4-4 2 4-3 3 3 7"/><circle cx="17" cy="3" r="1.5"/>',
    config:'<path d="M3 9 12 4l9 5-9 5-9-5Zm3 2v7c4 3 8 3 12 0v-7M21 10v8"/>',
    token:'<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M10 5h4m-4 14h4m-4-8 2 2 4-4"/>',
    history:'<path d="M4 4v5h5M4 9a8 8 0 1 1 0 7m8-9v5l3 2"/>',
    calibration:'<rect x="3" y="5" width="18" height="14" rx="7"/><rect x="6" y="8" width="12" height="8" rx="4"/><path d="M12 2v5m0 10v5"/>',
    lock:'<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/>',
    face:'<path d="M3 8V3h5m8 0h5v5M3 16v5h5m8 0h5v-5M8 15c2 3 6 3 8 0M8 9h1m6 0h1"/>',
    lab:'<path d="m3 17 5-6 4 3 8-10M16 4h4v4M3 3v18h18"/>',
    moon:'<path d="M20 14A8 8 0 0 1 10 4a8 8 0 1 0 10 10Z"/>',
    menu:'<path d="M4 6h16M4 12h16M4 18h16"/>',
    arrow:'<path d="M4 12h16m-6-6 6 6-6 6"/>',
    leaf:'<path d="M20 4C8 2 2 8 6 15s16 1 14-11ZM5 20 16 9"/>',
    empty:'<rect x="3" y="5" width="18" height="14" rx="7"/><path d="M8 12h8m-4-4v8"/>'
  };
  function icon(name) { return '<svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (paths[name] || paths.overview) + '</svg>'; }
  window.Studio = { icon: icon };
  try { if (new URLSearchParams(location.search).get("embedded") === "1") { document.documentElement.classList.add("embedded"); document.addEventListener("DOMContentLoaded", function () { document.body.classList.add("embedded"); }); } } catch (e) {}
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-icon]").forEach(function (el) { el.innerHTML = icon(el.dataset.icon); });
    document.querySelectorAll(".toast").forEach(function (el) { el.setAttribute("role", "status"); el.setAttribute("aria-live", "polite"); });
    var menu = document.getElementById("menu-btn");
    function closeMenu() { document.body.classList.remove("menu-open"); if (menu) menu.setAttribute("aria-expanded", "false"); }
    if (menu) menu.addEventListener("click", function () { var open = document.body.classList.toggle("menu-open"); menu.setAttribute("aria-expanded", String(open)); });
    document.addEventListener("click", function (e) { if (e.target.closest("[data-step]")) closeMenu(); else if (document.body.classList.contains("menu-open") && !e.target.closest(".sidebar,#menu-btn")) closeMenu(); });
    window.addEventListener("storage", function (e) { if (e.key === "yunrun-theme" && /^(light|dark)$/.test(e.newValue)) document.documentElement.dataset.theme = e.newValue; });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape") closeMenu(); });
    // Focus follows the existing confirmation dialog; submission stays in its original handler.
    var modal = document.getElementById("modal"), previousFocus;
    if (modal) {
      modal.setAttribute("role", "dialog"); modal.setAttribute("aria-modal", "true"); modal.setAttribute("aria-labelledby", "confirmation-title");
      var heading = modal.querySelector("h3"); if (heading) heading.id = "confirmation-title";
      new MutationObserver(function () {
        if (modal.classList.contains("open")) { previousFocus = document.activeElement; document.body.style.overflow = "hidden"; document.getElementById("btn-cancel-modal").focus(); }
        else { document.body.style.overflow = ""; if (previousFocus && previousFocus.isConnected) previousFocus.focus(); }
      }).observe(modal, { attributes:true, attributeFilter:["class"] });
      modal.addEventListener("keydown", function (e) {
        if (e.key === "Escape") document.getElementById("btn-cancel-modal").click();
        if (e.key !== "Tab") return;
        var targets = Array.from(modal.querySelectorAll('button:not(:disabled),a[href],input:not(:disabled),[tabindex="0"]')).filter(function (el) { return el.getClientRects().length; });
        var first = targets[0], last = targets[targets.length-1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      });
    }
    // Same-origin embedded console uses its natural height, without a second scrollbar.
    if (document.body.classList.contains("embedded") && parent !== window) {
      var lastHeight = 0;
      new ResizeObserver(function () {
        var height = Math.ceil(document.body.getBoundingClientRect().height);
        if (height !== lastHeight) { lastHeight = height; parent.postMessage({ type:"yunrun-ui-height", height:height }, location.origin); }
      }).observe(document.body);
    }
    window.addEventListener("message", function (e) {
      var frame = document.getElementById("run-frame");
      if (frame && e.origin === location.origin && e.source === frame.contentWindow && e.data && e.data.type === "yunrun-ui-height" && Number.isFinite(e.data.height)) frame.style.height = Math.max(650, Math.min(20000, e.data.height + 10)) + "px";
    });
  });
})();
