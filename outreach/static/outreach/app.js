/* CarePulse UI behaviours. Event delegation so HTMX-swapped content works without re-binding. */
(function () {
  "use strict";

  function closest(el, sel) { return el && el.closest ? el.closest(sel) : null; }

  // Quick-pick chips: <button data-fill="Fajuyi Park" data-target="#id_location_met">
  document.addEventListener("click", function (e) {
    var chip = closest(e.target, "[data-fill]");
    if (!chip) return;
    var input = document.querySelector(chip.getAttribute("data-target"));
    if (!input) return;
    input.value = chip.getAttribute("data-fill");
    input.dispatchEvent(new Event("input", { bubbles: true }));
    var group = closest(chip, ".chips");
    if (group) group.querySelectorAll(".chip").forEach(function (c) { c.classList.toggle("is-active", c === chip); });
  });

  // Keep chip highlight in sync when typing
  document.addEventListener("input", function (e) {
    var id = e.target.id;
    if (!id) return;
    document.querySelectorAll('[data-target="#' + id + '"]').forEach(function (c) {
      c.classList.toggle("is-active", c.getAttribute("data-fill") === e.target.value);
    });
  });

  // Copy to clipboard: <button data-copy="text"> or data-copy-target="#input"
  document.addEventListener("click", function (e) {
    var btn = closest(e.target, "[data-copy], [data-copy-target]");
    if (!btn) return;
    var text = btn.getAttribute("data-copy");
    if (text === null) {
      var src = document.querySelector(btn.getAttribute("data-copy-target"));
      text = src ? (src.value || src.textContent) : "";
    }
    var label = btn.querySelector("[data-copy-label]");
    var done = function () {
      if (!label) return;
      var original = label.textContent;
      label.textContent = "Copied!";
      setTimeout(function () { label.textContent = original; }, 1600);
    };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); });
    } else { fallbackCopy(text); done(); }
  });
  function fallbackCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text; ta.setAttribute("readonly", ""); ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (err) { /* ignore */ }
    document.body.removeChild(ta);
  }

  // After "Open in WhatsApp", draw attention to "I've sent it"
  document.addEventListener("click", function (e) {
    var link = closest(e.target, "[data-whatsapp-open]");
    if (!link) return;
    var card = closest(link, ".contact-card");
    if (card) card.classList.add("whatsapp-opened");
  });

  // Expand / collapse long message previews
  document.addEventListener("click", function (e) {
    var btn = closest(e.target, "[data-expand]");
    if (!btn) return;
    var box = closest(btn, ".next-msg");
    if (!box) return;
    var expanded = box.classList.toggle("expanded");
    btn.textContent = expanded ? "Show less" : "Show full message";
    btn.setAttribute("aria-expanded", expanded ? "true" : "false");
  });

  // Dropdown menus (<details class="menu">): close on outside click, Escape, or after choosing an item
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (!menu.contains(e.target) || closest(e.target, ".menu-item:not([data-copy])")) menu.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", function (e) {
    if (e.key !== "Escape") return;
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      menu.removeAttribute("open");
      var s = menu.querySelector("summary"); if (s) s.focus();
    });
  });

  // File drop zone: show chosen file name
  function describeFile(zone, input) {
    var name = zone.querySelector("[data-file-name]");
    var has = input.files && input.files.length > 0;
    zone.classList.toggle("has-file", has);
    if (name) name.textContent = has ? input.files[0].name : name.getAttribute("data-empty");
  }
  document.addEventListener("change", function (e) {
    var zone = closest(e.target, ".dropzone");
    if (zone && e.target.type === "file") describeFile(zone, e.target);
  });
  ["dragenter", "dragover"].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var zone = closest(e.target, ".dropzone"); if (zone) zone.classList.add("is-dragover");
    });
  });
  ["dragleave", "drop"].forEach(function (type) {
    document.addEventListener(type, function (e) {
      var zone = closest(e.target, ".dropzone"); if (zone) zone.classList.remove("is-dragover");
    });
  });

  // Preset value buttons: <button data-set-value="250" data-target="#id_max_per_hour">
  document.addEventListener("click", function (e) {
    var btn = closest(e.target, "[data-set-value]");
    if (!btn) return;
    var input = document.querySelector(btn.getAttribute("data-target"));
    if (input) input.value = btn.getAttribute("data-set-value");
  });

  // Prevent double submits on regular (non-HTMX) forms
  document.addEventListener("submit", function (e) {
    var form = e.target;
    if (form.hasAttribute("hx-post") || form.hasAttribute("data-allow-resubmit")) return;
    var btn = form.querySelector('button[type="submit"]:not([name])');
    if (!btn) return;
    if (form.dataset.submitting === "1") { e.preventDefault(); return; }
    form.dataset.submitting = "1";
    btn.setAttribute("aria-busy", "true");
    btn.classList.add("htmx-request");
  });
})();
