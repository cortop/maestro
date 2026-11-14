// Palette ranking: pure, DOM-free relevance scoring shared by palette.js and the node test.
(function (root) {
  function wordStart(text, w) {
    var i = text.indexOf(w);
    while (i !== -1) {
      if (i === 0 || /[\s\-_/.:]/.test(text.charAt(i - 1))) return true;
      i = text.indexOf(w, i + 1);
    }
    return false;
  }
  // Lower is better; -1 = no match. 0: first token equals the query, 1: text starts with it,
  // 2: a word starts with it, 3: substring elsewhere.
  function score(text, query) {
    text = String(text).toLowerCase();
    query = String(query).toLowerCase().trim();
    if (!query) return 0;
    var words = query.split(/\s+/);
    for (var k = 0; k < words.length; k++) if (text.indexOf(words[k]) === -1) return -1;
    if (text.split(/\s+/)[0] === query) return 0;
    if (text.indexOf(query) === 0) return 1;
    if (wordStart(text, query)) return 2;
    return 3;
  }
  // Indices of the matching texts, best score first, ties in input order.
  function rank(texts, query) {
    var out = [];
    texts.forEach(function (t, i) { var s = score(t, query); if (s >= 0) out.push({ i: i, s: s }); });
    out.sort(function (a, b) { return a.s - b.s || a.i - b.i; });
    return out.map(function (o) { return o.i; });
  }
  var api = { score: score, rank: rank };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.PaletteRank = api;
})(typeof window !== "undefined" ? window : this);
