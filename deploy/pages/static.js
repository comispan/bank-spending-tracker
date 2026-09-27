// Loaded only by the static snapshot (deploy/snapshot.py), never by the app.
(function () {
  var base = document.currentScript.getAttribute('data-base') || '';

  // Nothing here can be written to: there is no server behind the page. The
  // category <select>s submit on change through form.submit(), which fires no
  // submit event, so the controls themselves are disabled, not just the event.
  document.querySelectorAll('form[method=post]').forEach(function (form) {
    form.querySelectorAll('input, select, textarea, button').forEach(function (el) {
      el.disabled = true;
    });
    form.title = 'Read-only in the static demo';
    form.addEventListener('submit', function (e) { e.preventDefault(); });
  });

  // The Analytics month picker is a GET form. Each pre-rendered query lives in
  // a folder named by the first 12 hex digits of the SHA-1 of its canonical
  // form, which is what snapshot.py's canonical_query produces; a pick that was
  // never rendered lands on 404.html, which says so.
  var pick = document.querySelector('form.monthpick');
  if (pick && window.crypto && crypto.subtle) {
    pick.addEventListener('submit', function (e) {
      e.preventDefault();
      var q = [];
      new FormData(pick).forEach(function (v, k) {
        q.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
      });
      // Every month, or none, is the unfiltered page, which is the one saved.
      var boxes = pick.querySelectorAll('input[name=month]');
      if (!q.length || q.length === boxes.length) { location.href = base + '/analytics/'; return; }
      crypto.subtle.digest('SHA-1', new TextEncoder().encode(q.join('&'))).then(function (buf) {
        var hex = Array.prototype.map.call(new Uint8Array(buf), function (b) {
          return ('0' + b.toString(16)).slice(-2);
        }).join('');
        location.href = base + '/analytics/q/' + hex.slice(0, 12) + '/';
      });
    });
  }
})();
