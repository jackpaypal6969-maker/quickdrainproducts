/* Build your box (/build-your-box). Vanilla, CSP-safe (no inline handlers).
   Reads #box-config, keeps the fixture counts in localStorage, and drives the
   dose calendar + summary. Adding to cart is app.js's data-add-form flow: this
   file only keeps the hidden variant_id / qty / subscribe inputs correct.
   Arithmetic: one bottle = one drain-month, so a 12-pack is one drain-year and
   qty = drains. Motion is CSS (transform/opacity); this file toggles classes. */
(function () {
  'use strict';
  const cfgEl = document.getElementById('box-config');
  const root = document.querySelector('[data-box]');
  if (!cfgEl || !root) return;
  let cfg = {};
  try { cfg = JSON.parse(cfgEl.textContent || '{}'); } catch (e) { return; }

  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (sel, el) => (el || root).querySelector(sel);
  const $$ = (sel, el) => Array.from((el || root).querySelectorAll(sel));
  const money = (c) => '$' + (c / 100).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const plural = (n, word) => n + ' ' + word + (n === 1 ? '' : 's');
  const STORE = 'qd_box_v1';
  const MAX_EACH = +cfg.maxEach || 20;
  const STOCK = typeof cfg.stock === 'number' ? cfg.stock : 50;
  const MAX_TOTAL = Math.max(0, Math.min(+cfg.maxTotal || 50, STOCK));
  const STAGGER = 20, STAGGER_CAP = 220; // ms per cell, and the most any cell waits

  const rows = $$('[data-fixture]');
  const keys = rows.map((r) => r.getAttribute('data-fixture'));
  const counts = {};
  keys.forEach((k) => { counts[k] = 0; });
  let delivery = 'once';
  const subOn = !!cfg.subscriptionsEnabled && Array.isArray(cfg.intervals) && cfg.intervals.indexOf(12) !== -1;

  /* ------------------------------------------------------------ persistence */
  function load() {
    try {
      const saved = JSON.parse(localStorage.getItem(STORE) || 'null');
      if (saved && saved.counts) keys.forEach((k) => { counts[k] = clamp(saved.counts[k]); });
      if (saved && saved.delivery === 'sub' && subOn) delivery = 'sub';
    } catch (e) { /* private mode or blocked storage: start empty */ }
  }
  function save() {
    try { localStorage.setItem(STORE, JSON.stringify({ counts, delivery })); } catch (e) { /* noop */ }
  }
  function clamp(n) { n = parseInt(n, 10); if (isNaN(n)) n = 0; return Math.max(0, Math.min(n, MAX_EACH)); }

  /* ------------------------------------------------------------ elements */
  const els = {
    drains: $('[data-sum-drains]'), drainsWord: $('[data-sum-drains-word]'),
    month: $('[data-sum-month]'), year: $('[data-sum-year]'), boxes: $('[data-sum-boxes]'),
    priceOnce: $('[data-price-once]'), priceSub: $('[data-price-sub]'), savingsInline: $('[data-savings-inline]'),
    total: $('[data-total]'), totalLabel: $('[data-total-label]'), totalNote: $('[data-total-note]'),
    qty: $('[data-box-qty]'), subscribe: $('[data-box-subscribe]'), button: $('[data-add-button]'), hint: $('[data-box-hint]'),
    summary: $('#box-summary'), caption: $('[data-cal-caption]'), totals: $$('[data-cal-totals] .box-cal-total'),
    deliveryFs: $('[data-box-delivery]'),
  };

  function pop(el) {
    if (!el || reduced) return;
    el.classList.remove('is-pop'); void el.offsetWidth; el.classList.add('is-pop');
  }

  /* ------------------------------------------------------------ calendar */
  function paintRow(key, n, prev) {
    const row = $('[data-cal-row="' + key + '"]');
    if (!row) return;
    const on = n > 0, wasOn = prev > 0;
    const cells = $$('.box-cell', row);
    if (on !== wasOn) {
      cells.forEach((cell, i) => {
        // light left→right, dim right→left; the wait per cell is capped so a row never takes longer than ~a quarter second
        const order = on ? i : (cells.length - 1 - i);
        cell.style.transitionDelay = reduced ? '0ms' : Math.min(order * STAGGER, STAGGER_CAP) + 'ms';
        cell.classList.toggle('is-dosed', on);
      });
    }
    row.classList.toggle('is-on', on);
    const chip = $('[data-chip="' + key + '"]', row);
    if (chip) { const label = String(n); if (chip.textContent !== label) { chip.textContent = label; if (on && wasOn) pop(chip); } }
  }

  function paintTotals(drains) {
    els.totals.forEach((t) => { t.textContent = String(drains); t.classList.toggle('is-on', drains > 0); });
  }

  /* ------------------------------------------------------------ summary */
  function render(changedKey, prevCount) {
    const drains = keys.reduce((s, k) => s + counts[k], 0);
    const perYear = drains * 12;
    const once = drains * (+cfg.priceCents || 0);
    const sub = drains * (+cfg.subPriceCents || 0);
    const savings = once - sub;
    const subscribing = subOn && delivery === 'sub';

    rows.forEach((r) => {
      const k = r.getAttribute('data-fixture');
      const input = $('[data-count]', r);
      if (input && input.value !== String(counts[k])) input.value = String(counts[k]);
      r.classList.toggle('is-on', counts[k] > 0);
      const minus = $('[data-step="-1"]', r), plus = $('[data-step="1"]', r);
      if (minus) minus.disabled = counts[k] <= 0;
      if (plus) plus.disabled = counts[k] >= MAX_EACH;
      const live = $('[data-count-live]', r);
      const label = $('label', r);
      if (live && label) live.textContent = counts[k] + ' ' + label.textContent.toLowerCase();
    });
    if (changedKey) paintRow(changedKey, counts[changedKey], prevCount); // bulk changes paint their own rows
    paintTotals(drains);

    if (els.drains && els.drains.textContent !== String(drains)) { els.drains.textContent = String(drains); pop(els.drains); }
    if (els.drainsWord) els.drainsWord.textContent = drains === 1 ? 'drain' : 'drains';
    if (els.month) els.month.textContent = String(drains);
    if (els.year) els.year.textContent = String(perYear);
    if (els.boxes) els.boxes.textContent = String(drains);
    if (els.caption) els.caption.textContent = drains ? plural(drains, 'drain') + ' · ' + plural(drains, 'bottle') + ' a month · ' + perYear + ' a year' : 'No drains yet.';

    if (els.priceOnce) els.priceOnce.textContent = money(once);
    if (els.priceSub) els.priceSub.textContent = money(sub);
    if (els.savingsInline) els.savingsInline.textContent = savings > 0 ? 'save ' + money(savings) + ' a year' : '';

    const shown = subscribing ? sub : once;
    if (els.total && els.total.textContent !== money(shown)) { els.total.textContent = money(shown); pop(els.total); }
    if (els.totalLabel) els.totalLabel.textContent = subscribing ? 'Per year, billed every 12 months' : 'Total, one-time';
    if (els.totalNote) els.totalNote.textContent = drains ? (subscribing ? 'was ' + money(once) + ' · ' + money(Math.round(sub / perYear)) + ' per bottle' : plural(drains, 'box') + ' of 12 · ' + money(+cfg.perBottleCents || Math.round(once / Math.max(perYear, 1))) + ' per bottle') : '';

    // the form app.js will post: qty = drains (one 12-pack per drain), subscribe = 12 months or 0
    if (els.qty) els.qty.value = String(Math.max(drains, 1));
    if (els.subscribe) els.subscribe.value = subscribing ? '12' : '0';

    let hint = '';
    if (STOCK <= 0) hint = 'Sold out right now. Check back soon.';
    else if (drains === 0) hint = 'Add at least one drain to build a box.';
    else if (drains > MAX_TOTAL) hint = 'A cart holds up to ' + MAX_TOTAL + ' boxes. For more than ' + MAX_TOTAL + ' drains, call us and we will quote it.';
    if (els.button) els.button.disabled = !!hint;
    if (els.hint) { els.hint.textContent = hint || (subscribing ? 'Renews every 12 months at the same count. Cancel any time from your account.' : 'One delivery. Switch to the subscription later if you like.'); els.hint.classList.toggle('text-warning', drains > MAX_TOTAL); }
    if (els.summary) els.summary.classList.toggle('is-ready', !hint);

    $$('[data-preset]').forEach((b) => {
      let p = null; try { p = JSON.parse(b.getAttribute('data-preset') || 'null'); } catch (e) { p = null; }
      const match = !!p && keys.every((k) => (+p[k] || 0) === counts[k]);
      b.setAttribute('aria-pressed', String(match));
    });
    save();
  }

  function set(key, n, opts) {
    const prev = counts[key];
    counts[key] = clamp(n);
    if (counts[key] === prev && !(opts && opts.force)) return;
    render(key, prev);
  }

  /* ------------------------------------------------------------ events */
  root.addEventListener('click', (e) => {
    const step = e.target.closest('[data-step]');
    if (step) { const k = step.getAttribute('data-for'); set(k, counts[k] + parseInt(step.getAttribute('data-step'), 10)); return; }
    const preset = e.target.closest('[data-preset]');
    if (preset) {
      let p = null; try { p = JSON.parse(preset.getAttribute('data-preset') || 'null'); } catch (x) { p = null; }
      if (!p) return;
      const prev = Object.assign({}, counts);
      keys.forEach((k) => { counts[k] = clamp(p[k]); });
      keys.forEach((k, i) => {
        // rows light one after another so the preset reads as a sequence, not a flash
        const delay = reduced ? 0 : Math.min(i * 60, 300);
        if (delay) setTimeout(() => paintRow(k, counts[k], prev[k]), delay); else paintRow(k, counts[k], prev[k]);
      });
      render(null, null);
      return;
    }
    if (e.target.closest('[data-box-clear]')) {
      const prev = Object.assign({}, counts);
      keys.forEach((k) => { counts[k] = 0; });
      keys.forEach((k) => paintRow(k, 0, prev[k]));
      render(null, null);
    }
  });
  root.addEventListener('input', (e) => {
    const input = e.target.closest('[data-count]');
    if (!input) return;
    if (input.value === '') return; // mid-edit
    set(input.getAttribute('data-count'), input.value);
  });
  root.addEventListener('change', (e) => {
    const input = e.target.closest('[data-count]');
    if (input) { set(input.getAttribute('data-count'), input.value, { force: true }); return; }
    if (e.target.matches('input[name=delivery]')) { delivery = e.target.value === 'sub' ? 'sub' : 'once'; render(null, null); }
  });
  root.addEventListener('keydown', (e) => {
    const input = e.target.closest('[data-count]');
    if (input && e.key === 'Enter') { e.preventDefault(); input.blur(); }
  });

  /* ------------------------------------------------------------ boot */
  load();
  if (els.deliveryFs) {
    const radio = els.deliveryFs.querySelector('input[name=delivery][value="' + delivery + '"]');
    if (radio) radio.checked = true;
  }
  // First paint: restored rows light up left→right once, then the page is simply in the saved state.
  keys.forEach((k) => paintRow(k, counts[k], -1));
  render(null, null);
})();
