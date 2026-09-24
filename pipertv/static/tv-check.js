/* Which of the two interfaces this browser gets, decided before either loads.
 *
 * The page below is written for a browser of the last few years. A television's
 * own browser is usually not one: the set this was built against runs QtWebKit
 * 4.8 from 2012, which has no CSS custom properties, no calc(), no flexbox, and
 * no JavaScript newer than ES5 -- the interface would stop at its own splash
 * screen, which is exactly what it did.
 *
 * So this file runs first, plainly enough for anything to read it, and sends a
 * browser that cannot show the interface to the one written for it instead.
 * Nothing is sniffed: what matters is what the engine can do, and it is asked.
 */
(function () {
  var here = window.location.pathname || "";
  if (here.indexOf("/tv/classic") === 0) return;   // already there

  // documentElement, not body: this runs while the head is still being read.
  var modern = !!(window.fetch && window.Promise && window.URLSearchParams
                  && document.documentElement.classList);
  var variables = false;
  try {
    var style = document.createElement("div").style;
    style.setProperty("--piper-probe", "1px");
    variables = style.getPropertyValue("--piper-probe") === "1px";
  } catch (error) {
    variables = false;
  }
  if (modern && variables) return;

  window.location.replace("/tv/classic" + (window.location.search || ""));
})();
