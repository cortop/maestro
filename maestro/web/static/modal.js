// T-259: the action modal is a <dialog> fetched into #modal-host; nothing swaps that host.
function closeModal() {
  var d = document.getElementById("action-modal");
  if (d && d.open) d.close();
  var host = document.getElementById("modal-host");
  if (host) host.innerHTML = "";
}
function openModal() {
  var d = document.getElementById("action-modal");
  if (d && !d.open) d.showModal();
}
document.addEventListener("htmx:afterSwap", function (e) {
  if (e.detail.target && e.detail.target.id === "modal-host") openModal();
});
document.addEventListener("click", function (e) {
  var t = e.target;
  if (t.closest && t.closest("[data-modal-cancel]")) { closeModal(); return; }
  if (t.id === "action-modal") closeModal();  // a click on the backdrop lands on the dialog itself
});
document.addEventListener("cancel", function (e) {  // Escape
  if (e.target.id === "action-modal") { e.preventDefault(); closeModal(); }
}, true);
document.addEventListener("htmx:afterRequest", function (e) {
  var el = e.detail.elt;
  if (el && el.closest && el.closest("#action-modal") && e.detail.successful && e.detail.xhr.status === 200) closeModal();
});
