/* Build your box (/build-your-box). Vanilla, CSP-safe (no inline handlers).
   Reads #box-config, keeps the fixture counts in localStorage, and drives the
   bottle rack + summary. Adding to cart is app.js's data-add-form flow: this
   file only keeps the hidden variant_id / qty / subscribe inputs correct.
   Arithmetic: one bottle = one drain-month, so a month's box is drains bottles.
   The SKU is normally a single bottle (unitsPerPack 1); if the catalog swaps in
   a pack, qty becomes the packs needed to cover the bottles. Motion is CSS
   (transform/opacity); this file toggles classes. */
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
  const STORE = 'qd_box_v2';
  const UNITS = Math.max(1, +cfg.unitsPerPack || 1);
  const INTERVAL = Math.max(1, +cfg.interval || 1);
  const EVERY = INTERVAL === 1 ? 'month' : INTERVAL + ' months';
  const MAX_EACH = +cfg.maxEach || 20;
  const STOCK = typeof cfg.stock === 'number' ? cfg.stock : 50; // packs in stock
  const MAX_TOTAL = Math.max(0, Math.min(+cfg.maxTotal || 50, STOCK) * UNITS); // bottles a cart can hold
  const STAGGER = 22, STAGGER_CAP = 240; // ms per bottle, and the most any bottle waits

  const rows = $$('[data-fixture]');
  const keys = rows.map((r) => r.getAttribute('data-fixture'));
  const counts = {};
  keys.forEach((k) => { counts[k] = 0; });
  let delivery = 'once';
  const subOn = !!cfg.subscriptionsEnabled;

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
    month: $('[data-sum-month]'), year: $('[data-sum-year]'),
    priceOnce: $('[data-price-once]'), priceSub: $('[data-price-sub]'), savingsInline: $('[data-savings-inline]'),
    total: $('[data-total]'), totalLabel: $('[data-total-label]'), totalNote: $('[data-total-note]'),
    qty: $('[data-box-qty]'), subscribe: $('[data-box-subscribe]'), button: $('[data-add-button]'), hint: $('[data-box-hint]'),
    summary: $('#box-summary'), caption: $('[data-cal-caption]'), rackTotal: $('[data-cal-total]'),
    deliveryFs: $('[data-box-delivery]'),
  };

  function pop(el) {
    if (!el || reduced) return;
    el.classList.remove('is-pop'); void el.offsetWidth; el.classList.add('is-pop');
  }

  /* ------------------------------------------------------------ bottle rack */
  function paintRow(key, n, prev) {
    const row = $('[data-cal-row="' + key + '"]');
    if (!row) return;
    const was = Math.max(prev, 0);
    const cells = $$('.box-cell', row);
    cells.forEach((cell, i) => {
      const lit = i < n;
      if (lit === cell.classList.contains('is-dosed')) return;
      // bottles fill left→right and empty right→left; only the changed span waits, capped at ~a quarter second
      const order = lit ? i - was : (was - 1 - i);
      cell.style.transitionDelay = reduced ? '0ms' : Math.min(Math.max(order, 0) * STAGGER, STAGGER_CAP) + 'ms';
      cell.classList.toggle('is-dosed', lit);
    });
    row.classList.toggle('is-on', n > 0);
    const chip = $('[data-chip="' + key + '"]', row);
    if (chip) { const label = String(n); if (chip.textContent !== label) { chip.textContent = label; if (n > 0 && was > 0) pop(chip); } }
  }

  /* ------------------------------------------------------------ summary */
  function render(changedKey, prevCount) {
    const drains = keys.reduce((s, k) => s + counts[k], 0);
    const bottles = drains;
    const qty = Math.ceil(bottles / UNITS);
    const once = qty * (+cfg.priceCents || 0);
    const sub = qty * (+cfg.subPriceCents || 0);
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
    if (els.rackTotal) { els.rackTotal.textContent = plural(bottles, 'bottle'); els.rackTotal.classList.toggle('is-on', bottles > 0); }

    if (els.drains && els.drains.textContent !== String(drains)) { els.drains.textContent = String(drains); pop(els.drains); }
    if (els.drainsWord) els.drainsWord.textContent = drains === 1 ? 'drain' : 'drains';
    if (els.month) els.month.textContent = String(bottles);
    if (els.year) els.year.textContent = String(bottles * 12);
    if (els.caption) els.caption.textContent = drains ? plural(drains, 'drain') + ' · ' + plural(bottles, 'bottle') + ' a month' : 'No drains yet.';

    if (els.priceOnce) els.priceOnce.textContent = money(once);
    if (els.priceSub) els.priceSub.textContent = money(sub) + (INTERVAL === 1 ? '/mo' : '');
    if (els.savingsInline) els.savingsInline.textContent = savings > 0 ? 'save ' + money(savings) + ' every ' + EVERY : '';

    const shown = subscribing ? sub : once;
    if (els.total && els.total.textContent !== money(shown)) { els.total.textContent = money(shown); pop(els.total); }
    if (els.totalLabel) els.totalLabel.textContent = subscribing ? 'Every ' + EVERY + ', billed each delivery' : 'Total, one-time';
    if (els.totalNote) {
      if (!drains) els.totalNote.textContent = '';
      else if (subscribing) els.totalNote.textContent = 'was ' + money(once) + ' · ' + money(Math.round(sub / Math.max(bottles, 1))) + ' per bottle';
      else els.totalNote.textContent = plural(bottles, 'bottle') + ' · ' + money(+cfg.perBottleCents || Math.round(once / Math.max(bottles, 1))) + ' per bottle';
    }

    // the form app.js will post: qty = packs covering the bottles (one bottle each by default), subscribe = interval months or 0
    if (els.qty) els.qty.value = String(Math.max(qty, 1));
    if (els.subscribe) els.subscribe.value = subscribing ? String(INTERVAL) : '0';

    let hint = '';
    if (STOCK <= 0) hint = 'Sold out right now. Check back soon.';
    else if (drains === 0) hint = 'Add at least one drain to build a box.';
    else if (bottles > MAX_TOTAL) hint = 'A cart holds up to ' + MAX_TOTAL + ' bottles. For more than ' + MAX_TOTAL + ' drains, call us and we will quote it.';
    if (els.button) els.button.disabled = !!hint;
    if (els.hint) { els.hint.textContent = hint || (subscribing ? 'The same box ships every ' + EVERY + ' at the same count. Cancel any time from your account.' : 'One delivery. Switch to the subscription later if you like.'); els.hint.classList.toggle('text-warning', bottles > MAX_TOTAL); }
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
        // rows fill one after another so the preset reads as a sequence, not a flash
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
  // First paint: restored rows fill left→right once, then the page is simply in the saved state.
  keys.forEach((k) => paintRow(k, counts[k], 0));
  render(null, null);
})();
