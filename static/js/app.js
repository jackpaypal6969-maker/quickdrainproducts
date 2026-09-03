/* Quick Drain Products — storefront behaviour. Vanilla, no framework.
   Replaces the Radix primitives behind the ported shadcn/ui blocks:
   sheet (cart drawer, mobile nav), dialog (newsletter), accordion (FAQ),
   radio group (variant selector). Motion is CSS; this file only toggles state. */
(function () {
  'use strict';
  document.documentElement.classList.add('js');
  const cfgEl = document.getElementById('qd-config');
  const cfg = cfgEl ? JSON.parse(cfgEl.textContent || '{}') : {};
  const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  const money = (c) => '$' + (c / 100).toFixed(2);

  /* ---------------------------------------------------------------- analytics */
  function track(name, props) {
    try { if (window.posthog && typeof window.posthog.capture === 'function') window.posthog.capture(name, props || {}); } catch (e) { /* noop */ }
  }
  if (cfg.posthogKey) {
    const boot = () => { if (window.posthog && window.posthog.init && !window.posthog.__loaded) { window.posthog.init(cfg.posthogKey, { api_host: cfg.posthogHost, persistence: 'localStorage+cookie', autocapture: false, capture_pageview: true }); window.posthog.__loaded = true; } };
    if (window.posthog) boot(); else window.addEventListener('load', boot);
  }

  /* ------------------------------------------------------------------- toast */
  function toast(message, kind) {
    const root = document.getElementById('toast-root');
    if (!root) return;
    const el = document.createElement('div');
    el.className = 'alert pointer-events-auto shadow-card ' + (kind === 'error' ? 'alert-error' : 'alert-ok');
    el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    el.textContent = message;
    root.appendChild(el);
    setTimeout(() => { el.style.opacity = '0'; el.style.transition = 'opacity 200ms'; setTimeout(() => el.remove(), 220); }, 3800);
  }

  /* ------------------------------------------------------------ focus trap */
  const FOCUSABLE = 'a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
  let openLayer = null;
  function openLayerEl(el, opener, restoreTo) {
    if (openLayer) closeLayer();
    const overlay = $('[data-overlay-for="' + el.id + '"]');
    el.hidden = false; if (overlay) overlay.hidden = false;
    // next frame so the transition runs from the hidden state
    requestAnimationFrame(() => { el.setAttribute('data-open', 'true'); if (overlay) overlay.setAttribute('data-open', 'true'); });
    document.body.style.overflow = 'hidden';
    const active = document.activeElement && document.activeElement !== document.body ? document.activeElement : null;
    openLayer = { el, opener: opener || null, restoreTo: restoreTo || opener || active, overlay };
    if (opener && opener.hasAttribute('aria-expanded')) opener.setAttribute('aria-expanded', 'true');
    const first = $$(FOCUSABLE, el).find(n => n.offsetParent !== null);
    setTimeout(() => (first || el).focus({ preventScroll: true }), 30);
    if (overlay) overlay.addEventListener('click', closeLayer, { once: true });
  }
  function closeLayer() {
    if (!openLayer) return;
    const { el, opener, restoreTo, overlay } = openLayer;
    el.removeAttribute('data-open'); if (overlay) overlay.removeAttribute('data-open');
    document.body.style.overflow = '';
    const done = () => { el.hidden = true; if (overlay) overlay.hidden = true; };
    if (reduced) done(); else setTimeout(done, 260);
    if (opener && opener.hasAttribute('aria-expanded')) opener.setAttribute('aria-expanded', 'false');
    if (restoreTo && restoreTo.focus && document.contains(restoreTo)) restoreTo.focus({ preventScroll: true });
    openLayer = null;
  }
  document.addEventListener('keydown', (e) => {
    if (!openLayer) return;
    if (e.key === 'Escape') { e.preventDefault(); closeLayer(); return; }
    if (e.key === 'Tab') {
      const nodes = $$(FOCUSABLE, openLayer.el).filter(n => n.offsetParent !== null);
      if (!nodes.length) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    }
  });
  document.addEventListener('click', (e) => {
    const back = e.target.closest('[data-history-back]');
    if (back) { e.preventDefault(); if (history.length > 1) history.back(); else location.href = '/'; return; }
    const closer = e.target.closest('[data-close-sheet],[data-close-dialog]');
    if (closer) { if (!(closer.tagName === 'A' && closer.getAttribute('href'))) e.preventDefault(); closeLayer(); return; }
    const openCart = e.target.closest('[data-open-cart]');
    if (openCart) { e.preventDefault(); openLayerEl($('#cart-drawer'), openCart); return; }
    const openNav = e.target.closest('[data-open-nav]');
    if (openNav) { e.preventDefault(); openLayerEl($('#mobile-nav'), openNav); return; }
  });

  /* ------------------------------------------------------------ header */
  const header = $('[data-header]');
  if (header) {
    let ticking = false;
    const update = () => { header.classList.toggle('is-condensed', window.scrollY > 24); ticking = false; };
    window.addEventListener('scroll', () => { if (!ticking) { ticking = true; requestAnimationFrame(update); } }, { passive: true });
    update();
  }

  /* ------------------------------------------------------------ reveal */
  const revealables = $$('[data-reveal]');
  if (revealables.length && !reduced && 'IntersectionObserver' in window) {
    $$('[data-reveal-stagger]').forEach(parent => {
      $$(':scope > [data-reveal]', parent).forEach((child, i) => child.style.setProperty('--reveal-delay', (i * 60) + 'ms'));
    });
    const io = new IntersectionObserver((entries) => {
      entries.forEach(en => { if (en.isIntersecting) { en.target.classList.add('is-visible'); io.unobserve(en.target); } });
    }, { rootMargin: '0px 0px -8% 0px', threshold: 0.08 });
    revealables.forEach(el => io.observe(el));
  } else {
    revealables.forEach(el => el.classList.add('is-visible'));
  }

  /* ------------------------------------------------------------ accordion */
  $$('[data-accordion]').forEach(acc => {
    $$('[data-acc-panel]', acc).forEach(p => { if (p.getAttribute('data-open') !== 'true' && p.firstElementChild) p.firstElementChild.inert = true; });
    acc.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-acc-trigger]');
      if (!btn) return;
      const panel = document.getElementById(btn.getAttribute('aria-controls'));
      const open = btn.getAttribute('aria-expanded') === 'true';
      btn.setAttribute('aria-expanded', String(!open));
      if (panel) { panel.setAttribute('data-open', String(!open)); const inner = panel.firstElementChild; if (inner) inner.inert = open; }
    });
  });

  /* ------------------------------------------------------------ cart */
  const csrfHeaders = () => ({ 'X-CSRF-Token': cfg.csrf || '', 'X-Requested-With': 'fetch', 'Accept': 'application/json' });
  const badge = $('[data-cart-count]');
  function setCount(n) {
    if (!badge) return;
    const changed = String(n) !== badge.getAttribute('data-count');
    badge.textContent = n; badge.setAttribute('data-count', String(n));
    if (changed && n > 0) { badge.classList.remove('is-pop'); void badge.offsetWidth; badge.classList.add('is-pop'); }
  }
  function setDrawer(html) { const body = $('[data-cart-drawer-body]'); if (body && html != null) body.innerHTML = html; }
  function skeleton(on) { const t = $('[data-cart-totals]'); if (t) t.classList.toggle('skeleton', on); }

  async function postForm(url, data) {
    const body = new URLSearchParams(data);
    const res = await fetch(url, { method: 'POST', headers: Object.assign({ 'Content-Type': 'application/x-www-form-urlencoded' }, csrfHeaders()), body });
    let json = null; try { json = await res.json(); } catch (e) { /* ignore */ }
    return { ok: res.ok && json && json.ok !== false, json: json || {}, status: res.status };
  }

  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('[data-add-form]');
    if (!form) return;
    e.preventDefault();
    const btn = form.querySelector('[data-add-button]') || form.querySelector('button[type=submit]');
    const data = Object.fromEntries(new FormData(form).entries());
    if (btn) { btn.classList.add('is-busy'); btn.disabled = true; }
    try {
      const { ok, json } = await postForm('/cart/add', data);
      if (btn) { btn.classList.remove('is-busy'); btn.disabled = false; }
      if (ok) {
        setCount(json.count || 0); setDrawer(json.html);
        if (btn) { const label = btn.textContent; btn.classList.add('is-done'); btn.textContent = 'Added'; setTimeout(() => { btn.classList.remove('is-done'); btn.textContent = label; }, 1400); }
        track('add_to_cart', { variant_id: data.variant_id, qty: data.qty });
        openLayerEl($('#cart-drawer'), $('[data-open-cart]'), btn);
      } else {
        toast((json && json.message) || 'Could not add that to the cart.', 'error');
      }
    } catch (err) {
      if (btn) { btn.classList.remove('is-busy'); btn.disabled = false; }
      form.submit(); // graceful fallback: full-page POST
    }
  });

  let cartSeq = 0;
  document.addEventListener('click', async (e) => {
    const qbtn = e.target.closest('[data-cart-qty]');
    if (!qbtn) return;
    e.preventDefault();
    const line = qbtn.closest('[data-line-id]');
    if (!line || line.dataset.busy === '1') return;
    line.dataset.busy = '1';
    $$('[data-cart-qty]', line).forEach(b => { b.disabled = true; });
    const seq = ++cartSeq;
    const current = parseInt(line.querySelector('span[aria-live]').textContent, 10) || 0;
    const next = current + parseInt(qbtn.getAttribute('data-delta'), 10);
    skeleton(true);
    let result;
    try { result = await postForm(next <= 0 ? '/cart/remove' : '/cart/update', { item_id: qbtn.getAttribute('data-cart-qty'), qty: next }); }
    catch (err) { result = { ok: false, json: {} }; }
    if (seq !== cartSeq) return; // a newer request already replaced the drawer
    skeleton(false);
    if (result.ok) { setCount(result.json.count || 0); setDrawer(result.json.html); }
    else { line.dataset.busy = ''; $$('[data-cart-qty]', line).forEach(b => { b.disabled = false; }); toast('Could not update the cart.', 'error'); }
  });

  /* ------------------------------------------------------- variant selector */
  const selector = $('[data-variant-selector]');
  if (selector) {
    const price = $('[data-price]'), compare = $('[data-compare]'), per = $('[data-per-unit]'), stock = $('[data-stock]');
    const hiddenVariant = $('[data-selected-variant]'), addBtn = $('[data-add-button-main]'), notify = $('[data-notify-form]'), notifyVariant = $('[data-notify-variant]');
    const qtyInput = $('[data-qty-input]');
    const delivery = $('[data-delivery-selector]');
    const subInput = $('[data-subscribe-input]'), intervalWrap = $('[data-interval-wrap]'), intervalSel = $('[data-interval-select]'), hint = $('[data-interval-hint]');
    const oncePrice = $('[data-once-price]'), subPrice = $('[data-sub-price]'), subPct = $('[data-sub-percent]');
    let intervalTouched = false;
    let current = null;
    function subscribing() { const r = delivery && delivery.querySelector('input[name=delivery]:checked'); return !!(r && r.value === 'sub'); }
    function syncDelivery() {
      if (!delivery || !current) return;
      const on = subscribing();
      const d = current.dataset;
      if (intervalWrap) intervalWrap.hidden = !on;
      if (on && intervalSel && !intervalTouched && d.subRec) intervalSel.value = d.subRec;
      const months = on && intervalSel ? parseInt(intervalSel.value, 10) : 0;
      if (subInput) subInput.value = String(months || 0);
      if (hint && on) {
        const rec = parseInt(d.subRec, 10);
        hint.textContent = months === rec
          ? 'A ' + d.name.toLowerCase() + ' every ' + months + (months === 1 ? ' month' : ' months') + ' keeps one drain dosed continuously. Billed each delivery; cancel any time.'
          : 'Billed ' + money(+d.subPrice) + ' every ' + months + (months === 1 ? ' month' : ' months') + '. Cancel any time from your account.';
      }
      const shown = on ? +d.subPrice : +d.price;
      if (price) price.textContent = money(shown);
      if (per) per.textContent = (+d.units > 1) ? money(Math.round(shown / +d.units)) + ' per bottle' : 'per bottle';
      if (compare) { const cmp = on ? +d.price : (d.compare ? +d.compare : 0); compare.textContent = cmp ? money(cmp) : ''; compare.hidden = !cmp; }
    }
    if (delivery) delivery.addEventListener('change', (e) => { if (e.target.matches('[data-interval-select]')) intervalTouched = true; syncDelivery(); });
    function apply(input) {
      const d = input.dataset;
      current = input;
      if (price) price.textContent = money(+d.price);
      if (compare) { compare.textContent = d.compare ? money(+d.compare) : ''; compare.hidden = !d.compare; }
      if (per) per.textContent = (+d.units > 1) ? money(Math.round(+d.price / +d.units)) + ' per bottle' : 'per bottle';
      if (oncePrice) oncePrice.textContent = money(+d.price);
      if (subPrice && d.subPrice) subPrice.textContent = money(+d.subPrice);
      if (subPct && d.subPercent) subPct.textContent = d.subPercent;
      if (hiddenVariant) hiddenVariant.value = d.id;
      if (notifyVariant) notifyVariant.value = d.id;
      const s = parseInt(d.stock, 10);
      if (stock) {
        stock.className = 'badge ' + (s <= 0 ? 'badge-danger' : (s <= +d.low ? 'badge-warning' : 'badge-success'));
        stock.textContent = s <= 0 ? 'Sold out' : (s <= +d.low ? 'Only ' + s + ' left' : 'In stock');
      }
      if (addBtn) { addBtn.disabled = s <= 0; addBtn.textContent = s <= 0 ? 'Sold out' : 'Add to cart'; }
      if (notify) notify.hidden = s > 0;
      if (qtyInput) { qtyInput.max = Math.max(Math.min(s, 50), 1); if (+qtyInput.value > +qtyInput.max) qtyInput.value = qtyInput.max; }
      $$('[data-ledger-row]').forEach(r => r.classList.toggle('is-active', r.getAttribute('data-ledger-row') === d.id));
      syncDelivery();
    }
    selector.addEventListener('change', (e) => { if (e.target.matches('input[type=radio]')) apply(e.target); });
    const checked = selector.querySelector('input[type=radio]:checked') || selector.querySelector('input[type=radio]');
    if (checked) apply(checked);
    $$('[data-qty-step]').forEach(b => b.addEventListener('click', () => {
      const v = Math.min(Math.max((+qtyInput.value || 1) + (+b.getAttribute('data-qty-step')), 1), +qtyInput.max || 50);
      qtyInput.value = v;
    }));
  }

  /* ------------------------------------------------------------ gallery */
  const stage = $('[data-gallery-stage]');
  if (stage) {
    $$('[data-gallery-thumb]').forEach(t => t.addEventListener('click', () => {
      $$('[data-gallery-thumb]').forEach(x => x.setAttribute('aria-current', 'false'));
      t.setAttribute('aria-current', 'true');
      const target = document.getElementById(t.getAttribute('data-gallery-thumb'));
      $$('[data-gallery-item]', stage).forEach(item => { item.hidden = item !== target; });
      if (target) { target.classList.remove('gallery-fade'); void target.offsetWidth; target.classList.add('gallery-fade'); }
    }));
  }

  /* -------------------------------------------------------- ajax forms */
  document.addEventListener('submit', async (e) => {
    const form = e.target.closest('[data-ajax-form]');
    if (!form) return;
    e.preventDefault();
    const msg = form.querySelector('[data-form-message]');
    const btn = form.querySelector('button[type=submit]');
    if (btn) { btn.classList.add('is-busy'); btn.disabled = true; }
    try {
      const { ok, json } = await postForm(form.getAttribute('action'), Object.fromEntries(new FormData(form).entries()));
      if (btn) { btn.classList.remove('is-busy'); btn.disabled = false; }
      if (msg) { msg.textContent = json.message || (ok ? 'Done.' : 'Something went wrong.'); msg.className = 'text-xs mt-2 ' + (ok ? 'text-success' : 'text-danger'); }
      if (ok) {
        form.reset();
        if (form.hasAttribute('data-success-close')) { try { localStorage.setItem('qd_nl_done', '1'); } catch (x) { /* noop */ } setTimeout(closeLayer, 1500); }
      }
    } catch (err) {
      if (btn) { btn.classList.remove('is-busy'); btn.disabled = false; }
      form.submit();
    }
  });

  /* ------------------------------------------------------ newsletter modal */
  const nl = $('#newsletter-dialog');
  if (nl && !/^\/(cart|checkout|account|admin|orders|email|reviews)/.test(cfg.path || '')) {
    let seen = false;
    try { seen = !!localStorage.getItem('qd_nl_seen') || !!localStorage.getItem('qd_nl_done'); } catch (e) { seen = true; }
    if (!seen && !/[?&]nl=0/.test(location.search)) {
      let fired = false;
      const fire = () => {
        if (fired || openLayer) return;
        const a = document.activeElement;
        if (a && a.closest && a.closest('form')) { setTimeout(fire, 15000); return; } // someone is typing: try later
        fired = true;
        try { localStorage.setItem('qd_nl_seen', String(Date.now())); } catch (e) { /* noop */ }
        openLayerEl(nl, null);
      };
      setTimeout(fire, 22000);
      window.addEventListener('scroll', () => { if (window.scrollY > document.body.scrollHeight * 0.5) fire(); }, { passive: true });
    }
  }


  /* ------------------------------------------------------------ label reader (tabs) */
  $$('[data-label-reader]').forEach(root => {
    const tabs = $$('[data-label-tab]', root);
    const panels = $$('[data-label-panel]', root);
    function select(tab) {
      tabs.forEach(t => { const on = t === tab; t.setAttribute('aria-selected', String(on)); t.tabIndex = on ? 0 : -1; });
      panels.forEach(p => { p.hidden = p.id !== tab.getAttribute('aria-controls'); });
    }
    tabs.forEach(t => {
      t.addEventListener('click', () => select(t));
      t.addEventListener('mouseenter', () => { if (window.matchMedia('(hover: hover)').matches) select(t); });
      t.addEventListener('keydown', (e) => {
        const i = tabs.indexOf(t);
        if (e.key === 'ArrowDown' || e.key === 'ArrowRight') { e.preventDefault(); const n = tabs[(i + 1) % tabs.length]; select(n); n.focus(); }
        if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') { e.preventDefault(); const n = tabs[(i - 1 + tabs.length) % tabs.length]; select(n); n.focus(); }
      });
    });
  });

  /* ------------------------------------------------------ pending checkout */
  const pending = $('[data-pending-refresh]');
  if (pending) setTimeout(() => location.replace(pending.getAttribute('data-pending-refresh')), 3000);
})();
