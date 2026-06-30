"""Shared, dependency-free pieces for the runtime probe.

Imported by BOTH the in-server verifier (`backend.src.ai.verifier`) and the
out-of-process probe child (`runtime_probe_child.py`, launched by FILE PATH).
Keep this module free of heavy imports (no config / DB / httpx) so the child
stays light and can import it as a plain sibling module regardless of how the
parent package was loaded (`backend.src.ai...` in the server vs `src.ai...` in
tests).
"""
from __future__ import annotations

# How long we wait for the React tree to mount before declaring "blank page".
# Apps doing slow initial data fetching can take a couple of seconds.
MOUNT_TIMEOUT_MS = 8000

# Console messages that are dev-tooling / vite-HMR noise, not real bugs the AI
# should be asked to fix. Stripped before reporting.
RUNTIME_IGNORE_SUBSTRINGS = (
    "Download the React DevTools",
    "React DevTools",
    "[vite] connecting",
    "[vite] connected",
    "[HMR]",
    "[vite-plugin-react]",
    # Strict-mode duplicate render warnings — the template runs in StrictMode,
    # so these get logged but aren't actionable.
    "Warning: ReactDOM.render is no longer supported",
    "double-invoked",
)


def is_noise(message: str) -> bool:
    return any(s in message for s in RUNTIME_IGNORE_SUBSTRINGS)


# ---------- Accessibility audit (dependency-free, runs in the page) ----------
# A small, high-value WCAG rule-set evaluated in the live DOM via Playwright.
# Deliberately NOT axe-core: shipping a 550KB vendored JS for an on-prem,
# offline install is the wrong trade — this mirrors the security scanner's
# "explainable built-in rules" philosophy. Catches the issues that actually
# break screen-reader / keyboard users on generated apps.
A11Y_AUDIT_JS = r"""
() => {
  const out = [];
  const push = (rule, detail, el) => {
    if (out.length >= 30) return;
    let sel = '';
    if (el) {
      sel = el.tagName.toLowerCase();
      if (el.id) sel += '#' + el.id;
      else if (typeof el.className === 'string' && el.className.trim())
        sel += '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.');
    }
    out.push({ rule, detail, selector: sel });
  };
  const html = document.documentElement;
  if (!html.getAttribute('lang'))
    push('html-has-lang', 'The <html> element has no lang attribute', html);
  document.querySelectorAll('img').forEach((img) => {
    if (img.getAttribute('aria-hidden') === 'true' || img.getAttribute('role') === 'presentation') return;
    if (!img.hasAttribute('alt')) push('image-alt', 'Image is missing an alt attribute', img);
  });
  const accName = (el) =>
    (el.getAttribute('aria-label') || '').trim() ||
    (el.getAttribute('aria-labelledby') ? 'x' : '') ||
    (el.getAttribute('title') || '').trim() ||
    (el.textContent || '').trim() ||
    (el.querySelector('img[alt]') ? (el.querySelector('img[alt]').getAttribute('alt') || '').trim() : '');
  document.querySelectorAll('button, a[href]').forEach((el) => {
    if (el.getAttribute('aria-hidden') === 'true') return;
    if (!accName(el))
      push('control-name', el.tagName.toLowerCase() + ' has no accessible name (text, aria-label, or title)', el);
  });
  document.querySelectorAll('input, select, textarea').forEach((el) => {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'reset', 'image'].includes(type)) return;
    if (el.getAttribute('aria-label') || el.getAttribute('aria-labelledby') || el.getAttribute('title')) return;
    if (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) return;
    if (el.closest('label')) return;
    push('label', el.tagName.toLowerCase() + ' form control has no associated label', el);
  });
  const ids = {};
  document.querySelectorAll('[id]').forEach((el) => { if (el.id) ids[el.id] = (ids[el.id] || 0) + 1; });
  Object.keys(ids).forEach((id) => {
    if (ids[id] > 1) push('duplicate-id', 'Duplicate id="' + id + '" on ' + ids[id] + ' elements', null);
  });
  return out;
}
"""
