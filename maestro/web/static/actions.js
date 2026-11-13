// A refused action answers 409 with the refreshed fragment carrying the reason; swap it in.
document.addEventListener("htmx:beforeSwap", function (e) {
  if (e.detail.xhr.status === 409) { e.detail.shouldSwap = true; e.detail.isError = false; }
});
