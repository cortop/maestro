// Session transcript: keep the newest step in view while "follow" is on, and jump between error steps.
(function () {
  function follow(box) {
    var toggle = box.querySelector(".follow");
    var steps = box.querySelectorAll(".step");
    var seen = Number(box.dataset.seen || 0);
    box.dataset.seen = steps.length;
    if (toggle && toggle.checked && steps.length > seen) steps[steps.length - 1].scrollIntoView({ block: "end" });
  }
  document.addEventListener("htmx:afterSettle", function () {
    document.querySelectorAll(".transcript").forEach(follow);
  });
  document.addEventListener("click", function (ev) {
    var btn = ev.target.closest && ev.target.closest(".next-error");
    if (!btn) return;
    var box = btn.closest(".transcript");
    var errors = box.querySelectorAll(".step[data-error]");
    var status = box.querySelector(".next-error-status");
    var cursor = Number(box.dataset.errorCursor || -1);
    var next = null;
    for (var i = 0; i < errors.length; i++) {
      if (Number(errors[i].dataset.index) > cursor) { next = errors[i]; break; }
    }
    if (!next) { status.textContent = errors.length ? "no further errors" : "no errors"; return; }
    status.textContent = "";
    box.dataset.errorCursor = next.dataset.index;
    var toggle = box.querySelector(".follow");
    if (toggle) toggle.checked = false;  // a new step would otherwise snap the view back down
    next.scrollIntoView({ block: "center" });
  });
})();
