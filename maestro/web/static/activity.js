// Activity page pause control: while paused the feed ignores live updates (its hx-trigger
// filter reads window.maestroActivityPaused); resuming refreshes the feed at once.
(function () {
  window.maestroActivityPaused = false;
  var btn = document.getElementById("activity-pause");
  if (!btn) return;
  btn.addEventListener("click", function () {
    window.maestroActivityPaused = !window.maestroActivityPaused;
    var paused = window.maestroActivityPaused;
    btn.textContent = paused ? btn.dataset.resumeLabel : btn.dataset.pauseLabel;
    btn.setAttribute("aria-pressed", paused ? "true" : "false");
    if (!paused) htmx.trigger("#activity-rows", "sse:board");
  });
})();
