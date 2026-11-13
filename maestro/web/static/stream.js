// Event-stream lifecycle. A page the browser keeps in its back/forward cache keeps its /stream
// connection open, and a browser allows six connections per host: a handful of navigations would
// use them all and stall every later request. Close the stream when the page is left.
(function () {
  var sources = [];
  var create = htmx.createEventSource;
  htmx.createEventSource = function (url) {
    var source = create(url);
    sources.push(source);
    return source;
  };
  window.addEventListener("pagehide", function () {
    sources.forEach(function (source) { source.close(); });
    sources = [];
  });
  // A page restored from that cache has no stream and stale content: load it afresh.
  window.addEventListener("pageshow", function (ev) { if (ev.persisted) window.location.reload(); });
})();
