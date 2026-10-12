/*
 * StockFlow dashboard behaviour. Vanilla JS, no build step, no dependencies.
 *
 * Rules followed here:
 *  - All dynamic content is written with textContent / DOM creation (helper h()). No innerHTML anywhere.
 *  - Money is handled as integer cents in BigInt; floats are never used for money. The server is authoritative:
 *    the order preview mirrors server rules (discount tier on subtotal, tax on discounted subtotal, ROUND_HALF_UP)
 *    but the numbers shown after placing an order always come from the API response.
 *  - No inline event handlers; everything is wired with addEventListener / event delegation.
 */
(function () {
  'use strict';

  // ------------------------------------------------------------------ config
  var app = document.getElementById('app');
  if (!app) { return; }

  var API = document.body.dataset.apiBase || '/api';
  var DISCOUNT_TIERS = [[50000n, 10n], [10000n, 5n]]; // [subtotal cents threshold (inclusive), percent], highest first
  var MAX_QTY = 10000;
  var CATALOG_LIMIT = 200;

  var state = {
    threshold: parseInt(app.dataset.lowThreshold, 10) || 5,
    taxRate: parseRate(app.dataset.taxRate),
    products: [],      // rows currently shown in the inventory table
    catalog: [],       // all products (for the order builder and category lists)
    lines: [],         // order builder: {key, productId (number|null), qty (string)}
    nextKey: 1,
    editing: null,     // product being edited, or null when creating
    stockProduct: null,
    lastOrderId: null,
    listSeq: 0
  };

  // ------------------------------------------------------------------ DOM helpers
  function $(id) { return document.getElementById(id); }

  var PROP_KEYS = { value: 1, checked: 1, selected: 1, disabled: 1, hidden: 1 };

  /** Create an element. Strings become text nodes (never parsed as HTML). */
  function h(tag, props) {
    var el = document.createElement(tag);
    var key;
    if (props) {
      for (key in props) {
        if (!Object.prototype.hasOwnProperty.call(props, key)) { continue; }
        var v = props[key];
        if (v === null || v === undefined || v === false) { continue; }
        if (key === 'class') { el.className = v; }
        else if (key === 'text') { el.textContent = v; }
        else if (key === 'dataset') { Object.keys(v).forEach(function (k) { el.dataset[k] = String(v[k]); }); }
        else if (PROP_KEYS[key]) { el[key] = v; }
        else { el.setAttribute(key, v === true ? '' : String(v)); }
      }
    }
    for (var i = 2; i < arguments.length; i++) { appendChild(el, arguments[i]); }
    return el;
  }
  function appendChild(el, child) {
    if (child === null || child === undefined || child === false) { return; }
    if (Array.isArray(child)) { child.forEach(function (c) { appendChild(el, c); }); }
    else if (child instanceof Node) { el.appendChild(child); }
    else { el.appendChild(document.createTextNode(String(child))); }
  }
  function clear(el) { while (el.firstChild) { el.removeChild(el.firstChild); } }
  function setText(id, text) { var el = $(id); if (el) { el.textContent = text; } }

  // ------------------------------------------------------------------ money (integer cents, BigInt)
  /** "19.99" -> 1999n ; returns null when not a valid non-negative amount with <= 2 decimals. */
  function parseCents(str) {
    if (typeof str !== 'string') { return null; }
    var m = /^(\d{1,12})(?:\.(\d{1,2}))?$/.exec(str.trim());
    if (!m) { return null; }
    return BigInt(m[1]) * 100n + BigInt(((m[2] || '') + '00').slice(0, 2));
  }
  function groupDigits(s) { return s.replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
  function fmtCents(c) {
    var neg = c < 0n;
    var abs = neg ? -c : c;
    var whole = (abs / 100n).toString();
    var frac = (abs % 100n).toString().padStart(2, '0');
    return (neg ? '-' : '') + '$' + groupDigits(whole) + '.' + frac;
  }
  function fmtMoneyString(s) { var c = parseCents(s); return c === null ? String(s) : fmtCents(c); }
  /** Round-half-up integer division for non-negative n and positive d. */
  function roundDiv(n, d) { return (2n * n + d) / (2n * d); }
  /** "0.08" -> {num: 8n, den: 100n} */
  function parseRate(str) {
    var m = /^(\d+)(?:\.(\d+))?$/.exec(String(str || '').trim());
    if (!m || (m[2] || '').length > 8) { return { num: 8n, den: 100n }; }
    var frac = m[2] || '';
    return { num: BigInt(m[1] + frac), den: 10n ** BigInt(frac.length) };
  }
  function rateLabel(rate) {
    // percent with up to 2 decimals, derived with integers (num/den*100)
    var scaled = roundDiv(rate.num * 10000n, rate.den); // percent * 100
    var whole = scaled / 100n;
    var frac = scaled % 100n;
    return whole.toString() + (frac === 0n ? '' : '.' + frac.toString().padStart(2, '0').replace(/0$/, ''));
  }
  function computeTotals(lineTotals) {
    var subtotal = lineTotals.reduce(function (a, b) { return a + b; }, 0n);
    var pct = 0n;
    for (var i = 0; i < DISCOUNT_TIERS.length; i++) {
      if (subtotal >= DISCOUNT_TIERS[i][0]) { pct = DISCOUNT_TIERS[i][1]; break; }
    }
    var discount = roundDiv(subtotal * pct, 100n);
    var taxable = subtotal - discount;
    var tax = roundDiv(taxable * state.taxRate.num, state.taxRate.den);
    return { subtotal: subtotal, discountPct: pct, discount: discount, tax: tax, total: subtotal - discount + tax };
  }

  // ------------------------------------------------------------------ misc formatting
  function stockState(qty) {
    if (qty <= 0) { return { cls: 'badge-danger', label: 'Out of stock' }; }
    if (qty <= state.threshold) { return { cls: 'badge-warn', label: 'Low stock' }; }
    return { cls: 'badge-ok', label: 'In stock' };
  }
  function fmtDate(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) { return String(iso); }
    try { return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' }); }
    catch (e) { return d.toISOString(); }
  }
  function badge(cls, label) { return h('span', { class: 'badge ' + cls }, label); }

  // ------------------------------------------------------------------ toasts
  function toast(message, isError) {
    var box = $('toasts');
    var t = h('div', { class: 'toast' + (isError ? ' toast-error' : '') }, message);
    box.appendChild(t);
    setTimeout(function () { if (t.parentNode) { t.parentNode.removeChild(t); } }, isError ? 9000 : 5000);
  }

  // ------------------------------------------------------------------ API
  function ApiError(status, code, message, details) {
    this.status = status; this.code = code; this.message = message; this.details = details || [];
  }

  function api(method, path, body) {
    var opts = { method: method, headers: { 'Accept': 'application/json' } };
    if (body !== undefined) {
      opts.headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    return fetch(API + path, opts).then(function (res) {
      return res.text().then(function (text) {
        var data = null;
        if (text) { try { data = JSON.parse(text); } catch (e) { data = null; } }
        if (res.ok) { return data; }
        if (data && data.error) {
          throw new ApiError(res.status, data.error.code, data.error.message || 'Request failed.', data.error.details);
        }
        throw new ApiError(res.status, 'http_' + res.status, 'Request failed (HTTP ' + res.status + ').', []);
      });
    }, function () {
      throw new ApiError(0, 'network_error', 'Network error: could not reach the server.', []);
    });
  }

  function detailLines(err) {
    var out = [];
    (err.details || []).forEach(function (d) {
      if (typeof d === 'string') { out.push(d); return; }
      if (!d || typeof d !== 'object') { return; }
      switch (err.code) {
        case 'insufficient_stock':
          out.push((d.sku || ('Product #' + d.product_id)) + ': requested ' + d.requested + ', only ' + d.available + ' available');
          break;
        case 'product_inactive':
          out.push((d.sku || ('Product #' + d.product_id)) + ' is inactive and cannot be ordered');
          break;
        case 'product_not_found':
          out.push('Product #' + d.product_id + ' no longer exists');
          break;
        case 'negative_stock':
          out.push('Current stock ' + d.current + ', change ' + d.delta + ' would go below zero');
          break;
        case 'duplicate_sku':
          out.push('SKU ' + d.sku + ' is already used');
          break;
        case 'validation_error':
          out.push((d.field ? d.field + ': ' : '') + (d.message || 'invalid value'));
          break;
        default:
          out.push(Object.keys(d).map(function (k) { return k + ': ' + d[k]; }).join(', '));
      }
    });
    return out;
  }

  /** Show an ApiError (or any Error) in a notice box with ids <prefix>-error[-msg|-details]. */
  function showError(prefix, err) {
    var box = $(prefix + '-error');
    setText(prefix + '-error-msg', err && err.message ? err.message : 'Something went wrong.');
    var list = $(prefix + '-error-details');
    if (list) {
      clear(list);
      detailLines(err || {}).forEach(function (line) { list.appendChild(h('li', null, line)); });
    }
    box.hidden = false;
  }
  function hideError(prefix) {
    var box = $(prefix + '-error');
    if (box) { box.hidden = true; }
    var list = $(prefix + '-error-details');
    if (list) { clear(list); }
  }

  // ------------------------------------------------------------------ metrics
  function renderMetrics(m) {
    state.threshold = m.low_stock_threshold;
    setText('m-revenue', fmtMoneyString(m.revenue_total));
    setText('m-orders', String(m.order_count));
    setText('m-inventory', fmtMoneyString(m.inventory_value));
    setText('m-low', String(m.low_stock_count));
    setText('m-out', String(m.out_of_stock_count));
    setText('m-active', String(m.active_product_count));
    setText('m-products', String(m.product_count));
    setText('m-threshold', String(m.low_stock_threshold));
  }
  function loadMetrics() {
    return api('GET', '/metrics').then(renderMetrics).catch(function () { /* cards keep server-rendered values */ });
  }

  // ------------------------------------------------------------------ inventory table
  function filterQuery() {
    var p = new URLSearchParams();
    var q = $('f-q').value.trim();
    if (q) { p.set('q', q); }
    if ($('f-category').value) { p.set('category', $('f-category').value); }
    if ($('f-active').value) { p.set('active', $('f-active').value); }
    if ($('f-low').checked) { p.set('low_stock', 'true'); }
    p.set('limit', String(CATALOG_LIMIT));
    return p.toString();
  }
  function filtersActive() {
    return !!($('f-q').value.trim() || $('f-category').value || $('f-active').value || $('f-low').checked);
  }

  function productRow(p) {
    var st = stockState(p.quantity_in_stock);
    var actions = h('td', { class: 'actions' },
      h('button', { type: 'button', class: 'btn btn-sm', dataset: { action: 'edit', id: p.id }, 'aria-label': 'Edit ' + p.name }, 'Edit'),
      h('button', { type: 'button', class: 'btn btn-sm', dataset: { action: 'stock', id: p.id }, 'aria-label': 'Adjust stock for ' + p.name }, 'Stock'),
      p.active ? h('button', { type: 'button', class: 'btn btn-sm btn-primary-outline', dataset: { action: 'add-to-order', id: p.id }, 'aria-label': 'Add ' + p.name + ' to order' }, 'Add to order') : null
    );
    return h('tr', { class: p.active ? null : 'is-inactive', dataset: { id: p.id } },
      h('td', { class: 'mono', dataset: { label: 'SKU' } }, p.sku),
      h('td', { dataset: { label: 'Product' } }, h('span', { class: 'strong' }, p.name), p.description ? h('span', { class: 'sub' }, p.description) : null),
      h('td', { dataset: { label: 'Category' } }, p.category),
      h('td', { class: 'num', dataset: { label: 'Price' } }, fmtMoneyString(p.price)),
      h('td', { class: 'num', dataset: { label: 'Stock' } }, h('span', { class: 'qty' }, String(p.quantity_in_stock)), ' ', badge(st.cls, st.label)),
      h('td', { dataset: { label: 'Status' } }, p.active ? badge('badge-ok', 'Active') : badge('badge-muted', 'Inactive')),
      actions
    );
  }

  function renderProducts(items, total) {
    var body = $('products-body');
    clear(body);
    items.forEach(function (p) { body.appendChild(productRow(p)); });
    $('products-table').hidden = items.length === 0;
    $('products-empty').hidden = items.length !== 0;
    $('products-error').hidden = true;
    var msg = 'Showing ' + items.length + (total > items.length ? ' of ' + total : '') + ' product' + (items.length === 1 ? '' : 's');
    setText('products-count', msg + (filtersActive() ? ' (filtered)' : ''));
  }

  function loadProducts() {
    var seq = ++state.listSeq;
    var region = $('products-region');
    region.setAttribute('aria-busy', 'true');
    return api('GET', '/products?' + filterQuery()).then(function (data) {
      if (seq !== state.listSeq) { return; } // a newer request superseded this one
      state.products = data.items;
      renderProducts(data.items, data.total);
    }).catch(function (err) {
      if (seq !== state.listSeq) { return; }
      $('products-table').hidden = true;
      $('products-empty').hidden = true;
      setText('products-error-msg', err.message);
      $('products-error').hidden = false;
      setText('products-count', 'Products could not be loaded.');
    }).then(function () {
      if (seq === state.listSeq) { region.setAttribute('aria-busy', 'false'); }
    });
  }

  function loadCatalog() {
    return api('GET', '/products?limit=' + CATALOG_LIMIT).then(function (data) {
      state.catalog = data.items;
      refreshCategoryControls();
      refreshLineSelects();
    }).catch(function () { /* builder will retry on next refresh */ });
  }

  function refreshCategoryControls() {
    var cats = {};
    state.catalog.forEach(function (p) { cats[p.category] = true; });
    var names = Object.keys(cats).sort(function (a, b) { return a.localeCompare(b); });
    var sel = $('f-category');
    var current = sel.value;
    clear(sel);
    sel.appendChild(h('option', { value: '' }, 'All categories'));
    names.forEach(function (c) { sel.appendChild(h('option', { value: c }, c)); });
    if (current && names.indexOf(current) === -1) { sel.appendChild(h('option', { value: current }, current)); }
    sel.value = current;
    var dl = $('category-options');
    clear(dl);
    names.forEach(function (c) { dl.appendChild(h('option', { value: c })); });
  }

  function findProduct(id) {
    var i;
    for (i = 0; i < state.catalog.length; i++) { if (state.catalog[i].id === id) { return state.catalog[i]; } }
    for (i = 0; i < state.products.length; i++) { if (state.products[i].id === id) { return state.products[i]; } }
    return null;
  }

  // ------------------------------------------------------------------ recent orders
  function orderRow(o) {
    return h('tr', { dataset: { id: o.id } },
      h('td', { class: 'strong', dataset: { label: 'Order' } }, '#' + o.id),
      h('td', { dataset: { label: 'Placed' } }, h('time', { datetime: o.created_at }, fmtDate(o.created_at))),
      h('td', { class: 'num', dataset: { label: 'Items' } }, String(o.line_items.length)),
      h('td', { class: 'num strong', dataset: { label: 'Total' } }, fmtMoneyString(o.total)),
      h('td', { class: 'actions' },
        h('button', { type: 'button', class: 'btn btn-sm', dataset: { action: 'view-order', id: o.id }, 'aria-label': 'View order ' + o.id }, 'View'))
    );
  }
  function loadOrders() {
    var region = $('orders-region');
    region.setAttribute('aria-busy', 'true');
    return api('GET', '/orders?limit=10').then(function (data) {
      var body = $('orders-body');
      clear(body);
      data.items.forEach(function (o) { body.appendChild(orderRow(o)); });
      $('orders-table').hidden = data.items.length === 0;
      $('orders-empty').hidden = data.items.length !== 0;
      $('orders-error').hidden = true;
    }).catch(function (err) {
      $('orders-table').hidden = true;
      $('orders-empty').hidden = true;
      setText('orders-error-msg', err.message);
      $('orders-error').hidden = false;
    }).then(function () { region.setAttribute('aria-busy', 'false'); });
  }

  function refreshAll() {
    return Promise.all([loadProducts(), loadCatalog(), loadMetrics(), loadOrders()]);
  }

  // ------------------------------------------------------------------ dialogs
  var lastOpener = null;
  function openDialog(dlg) {
    lastOpener = document.activeElement;
    if (typeof dlg.showModal === 'function') { dlg.showModal(); } else { dlg.setAttribute('open', ''); }
  }
  function closeDialog(dlg) {
    if (typeof dlg.close === 'function') { dlg.close(); } else { dlg.removeAttribute('open'); }
    if (lastOpener && document.contains(lastOpener) && lastOpener.focus) { lastOpener.focus(); }
  }
  function wireDialog(dlg) {
    dlg.addEventListener('click', function (ev) {
      if (ev.target === dlg) {
        // A click whose target is the <dialog> itself is either the backdrop or the dialog's own padding;
        // only close when the pointer is outside the dialog box.
        var r = dlg.getBoundingClientRect();
        var outside = ev.clientX < r.left || ev.clientX > r.right || ev.clientY < r.top || ev.clientY > r.bottom;
        if (outside) { closeDialog(dlg); }
      } else if (ev.target.closest && ev.target.closest('[data-close]')) { closeDialog(dlg); }
    });
  }

  // ---- product create / edit
  var PRODUCT_FIELDS = ['sku', 'name', 'description', 'price', 'category', 'active'];

  function openProductDialog(product) {
    state.editing = product || null;
    var form = $('product-form');
    form.reset();
    hideError('product');
    clearInvalid(form);
    setText('product-dialog-title', product ? 'Edit product' : 'New product');
    setText('product-submit', product ? 'Save changes' : 'Create product');
    $('pf-qty-field').hidden = !!product;
    $('pf-stock-note').hidden = !product;
    if (product) {
      $('pf-sku').value = product.sku;
      $('pf-name').value = product.name;
      $('pf-description').value = product.description;
      $('pf-price').value = product.price;
      $('pf-category').value = product.category;
      $('pf-active').checked = product.active;
    } else {
      $('pf-qty').value = '0';
      $('pf-category').value = 'General';
      $('pf-active').checked = true;
    }
    openDialog($('product-dialog'));
    $('pf-sku').focus();
  }

  function clearInvalid(form) {
    Array.prototype.forEach.call(form.elements, function (el) { el.removeAttribute('aria-invalid'); });
  }
  function markInvalidFields(form, err) {
    var map = { sku: 'pf-sku', name: 'pf-name', description: 'pf-description', price: 'pf-price', category: 'pf-category', quantity_in_stock: 'pf-qty', active: 'pf-active', delta: 'sf-delta', reason: 'sf-reason' };
    (err.details || []).forEach(function (d) {
      if (!d || typeof d.field !== 'string') { return; }
      var parts = d.field.split('.');
      var last = parts[parts.length - 1];
      if (map[last] && $(map[last])) { $(map[last]).setAttribute('aria-invalid', 'true'); }
    });
    if (err.code === 'duplicate_sku' && $('pf-sku')) { $('pf-sku').setAttribute('aria-invalid', 'true'); }
  }

  function submitProduct(ev) {
    ev.preventDefault();
    var form = $('product-form');
    hideError('product');
    clearInvalid(form);

    var priceStr = $('pf-price').value.trim();
    var cents = parseCents(priceStr);
    var problems = [];
    var sku = $('pf-sku').value.trim();
    var name = $('pf-name').value.trim();
    var category = $('pf-category').value.trim();
    if (!/^[A-Za-z0-9][A-Za-z0-9_-]{1,31}$/.test(sku)) { problems.push('SKU must be 2-32 characters: letters, digits, - or _.'); $('pf-sku').setAttribute('aria-invalid', 'true'); }
    if (!name) { problems.push('Name is required.'); $('pf-name').setAttribute('aria-invalid', 'true'); }
    if (cents === null) { problems.push('Price must be a number with at most 2 decimal places.'); $('pf-price').setAttribute('aria-invalid', 'true'); }
    if (!category) { problems.push('Category is required.'); $('pf-category').setAttribute('aria-invalid', 'true'); }
    var qty = 0;
    if (!state.editing) {
      var q = $('pf-qty').value.trim();
      if (!/^\d+$/.test(q)) { problems.push('Opening stock must be a whole number, 0 or more.'); $('pf-qty').setAttribute('aria-invalid', 'true'); }
      else { qty = parseInt(q, 10); }
    }
    if (problems.length) {
      showError('product', new ApiError(0, 'validation_error', 'Please fix the highlighted fields.', problems));
      var firstBad = form.querySelector('[aria-invalid="true"]');
      if (firstBad) { firstBad.focus(); }
      return;
    }

    var request;
    var normalizedPrice = (cents / 100n).toString() + '.' + (cents % 100n).toString().padStart(2, '0');
    if (state.editing) {
      var old = state.editing;
      var changes = {};
      if (sku.toUpperCase() !== old.sku) { changes.sku = sku; }
      if (name !== old.name) { changes.name = name; }
      if ($('pf-description').value !== old.description) { changes.description = $('pf-description').value; }
      if (cents !== parseCents(old.price)) { changes.price = normalizedPrice; }
      if (category !== old.category) { changes.category = category; }
      if ($('pf-active').checked !== old.active) { changes.active = $('pf-active').checked; }
      if (Object.keys(changes).length === 0) {
        showError('product', new ApiError(0, 'no_changes', 'Nothing has changed.', []));
        return;
      }
      request = api('PATCH', '/products/' + old.id, changes);
    } else {
      request = api('POST', '/products', {
        sku: sku, name: name, description: $('pf-description').value, price: normalizedPrice,
        quantity_in_stock: qty, category: category, active: $('pf-active').checked
      });
    }

    var btn = $('product-submit');
    btn.disabled = true;
    var editing = !!state.editing;
    request.then(function (saved) {
      closeDialog($('product-dialog'));
      toast((editing ? 'Updated ' : 'Created ') + saved.sku + ' - ' + saved.name + '.');
      return refreshAll().then(function () { flashRow('products-body', saved.id); });
    }).catch(function (err) {
      showError('product', err);
      markInvalidFields(form, err);
    }).then(function () { btn.disabled = false; });
  }

  function flashRow(bodyId, id) {
    var rows = $(bodyId).children;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].dataset.id === String(id)) {
        rows[i].classList.add('flash');
        setTimeout(function (r) { r.classList.remove('flash'); }.bind(null, rows[i]), 1300);
        return;
      }
    }
  }

  // ---- stock adjustment
  function openStockDialog(product) {
    state.stockProduct = product;
    $('stock-form').reset();
    hideError('stock');
    clearInvalid($('stock-form'));
    setText('stock-product', product.sku + ' - ' + product.name);
    setText('stock-current', 'Current stock: ' + product.quantity_in_stock);
    setText('sf-result', '');
    openDialog($('stock-dialog'));
    $('sf-delta').focus();
  }
  function previewStock() {
    var p = state.stockProduct;
    var raw = $('sf-delta').value.trim();
    if (!p || !/^-?\d+$/.test(raw)) { setText('sf-result', ''); return; }
    var next = p.quantity_in_stock + parseInt(raw, 10);
    setText('sf-result', next < 0 ? 'This would drop stock below zero and will be rejected.' : 'New stock level: ' + next);
  }
  function submitStock(ev) {
    ev.preventDefault();
    hideError('stock');
    clearInvalid($('stock-form'));
    var raw = $('sf-delta').value.trim();
    var delta = /^-?\d+$/.test(raw) ? parseInt(raw, 10) : NaN;
    if (!isFinite(delta) || delta === 0 || Math.abs(delta) > 1000000) {
      $('sf-delta').setAttribute('aria-invalid', 'true');
      showError('stock', new ApiError(0, 'validation_error', 'Enter a non-zero whole number (up to 1,000,000).', []));
      $('sf-delta').focus();
      return;
    }
    var body = { delta: delta };
    var reason = $('sf-reason').value.trim();
    if (reason) { body.reason = reason; }
    var btn = $('stock-submit');
    btn.disabled = true;
    api('POST', '/products/' + state.stockProduct.id + '/stock', body).then(function (saved) {
      closeDialog($('stock-dialog'));
      toast(saved.sku + ' stock is now ' + saved.quantity_in_stock + '.');
      return refreshAll().then(function () { flashRow('products-body', saved.id); });
    }).catch(function (err) {
      showError('stock', err);
      markInvalidFields($('stock-form'), err);
    }).then(function () { btn.disabled = false; });
  }

  // ---- order detail
  function openOrderDialog(id) {
    var dlg = $('order-dialog');
    setText('order-dialog-title', 'Order #' + id);
    setText('od-meta', 'Loading...');
    clear($('od-body'));
    clear($('od-totals'));
    $('od-error').hidden = true;
    $('od-table').hidden = false;
    openDialog(dlg);
    api('GET', '/orders/' + id).then(function (o) {
      setText('od-meta', 'Placed ' + fmtDate(o.created_at) + ' - status: ' + o.status);
      var body = $('od-body');
      o.line_items.forEach(function (li) {
        body.appendChild(h('tr', null,
          h('td', { class: 'mono', dataset: { label: 'SKU' } }, li.sku),
          h('td', { dataset: { label: 'Product' } }, li.product_name),
          h('td', { class: 'num', dataset: { label: 'Qty' } }, String(li.quantity)),
          h('td', { class: 'num', dataset: { label: 'Unit price' } }, fmtMoneyString(li.unit_price)),
          h('td', { class: 'num', dataset: { label: 'Line total' } }, fmtMoneyString(li.line_total))));
      });
      var t = $('od-totals');
      [['Subtotal', o.subtotal], ['Discount', o.discount], ['Tax', o.tax]].forEach(function (r) {
        t.appendChild(h('div', null, h('dt', null, r[0]), h('dd', null, fmtMoneyString(r[1]))));
      });
      t.appendChild(h('div', { class: 'totals-grand' }, h('dt', null, 'Total'), h('dd', null, fmtMoneyString(o.total))));
    }).catch(function (err) {
      setText('od-meta', '');
      $('od-table').hidden = true;
      var box = $('od-error');
      box.textContent = err.message;
      box.hidden = false;
    });
  }

  // ------------------------------------------------------------------ order builder
  function newLine(productId, qty) {
    return { key: state.nextKey++, productId: productId === undefined ? null : productId, qty: qty || '1' };
  }
  function orderableProducts() { return state.catalog.filter(function (p) { return p.active; }); }

  function parseQty(s) {
    if (!/^\d+$/.test(String(s).trim())) { return null; }
    var n = parseInt(s, 10);
    return n >= 1 && n <= MAX_QTY ? n : null;
  }

  function lineSelect(line) {
    var sel = h('select', { id: 'line-p-' + line.key, class: 'line-product', dataset: { key: line.key } },
      h('option', { value: '' }, 'Choose a product...'));
    fillSelect(sel, line);
    return sel;
  }
  function fillSelect(sel, line) {
    var taken = {};
    state.lines.forEach(function (l) { if (l.key !== line.key && l.productId !== null) { taken[l.productId] = true; } });
    var keepFirst = sel.firstChild;
    while (sel.lastChild !== keepFirst) { sel.removeChild(sel.lastChild); }
    orderableProducts().forEach(function (p) {
      var label = p.sku + ' - ' + p.name + ' (' + fmtMoneyString(p.price) + ', ' + p.quantity_in_stock + ' in stock)';
      sel.appendChild(h('option', { value: String(p.id), disabled: !!taken[p.id], selected: line.productId === p.id }, label));
    });
    sel.value = line.productId === null ? '' : String(line.productId);
  }
  function refreshLineSelects() {
    state.lines.forEach(function (line) {
      var sel = $('line-p-' + line.key);
      if (sel) { fillSelect(sel, line); }
    });
    updatePreview();
  }

  function renderLines(focusKey) {
    var list = $('order-lines');
    clear(list);
    state.lines.forEach(function (line, idx) {
      var n = idx + 1;
      list.appendChild(h('li', { class: 'line', dataset: { key: line.key } },
        h('div', { class: 'field line-select' },
          h('label', { for: 'line-p-' + line.key }, 'Product (line ' + n + ')'),
          lineSelect(line)),
        h('div', { class: 'field' },
          h('label', { for: 'line-q-' + line.key }, 'Quantity'),
          h('input', { id: 'line-q-' + line.key, class: 'line-qty', type: 'number', min: '1', max: String(MAX_QTY), step: '1', inputmode: 'numeric', value: line.qty, dataset: { key: line.key } })),
        h('button', { type: 'button', class: 'btn btn-sm btn-remove', dataset: { action: 'remove-line', key: line.key }, 'aria-label': 'Remove line ' + n }, 'Remove'),
        h('div', { class: 'line-info', id: 'line-i-' + line.key, 'aria-live': 'polite' })));
    });
    $('order-empty').hidden = state.lines.length !== 0;
    updatePreview();
    if (focusKey) {
      var el = $('line-p-' + focusKey);
      if (el) { el.focus(); }
    }
  }

  function updateLineInfo(line) {
    var info = $('line-i-' + line.key);
    if (!info) { return; }
    clear(info);
    var p = line.productId === null ? null : findProduct(line.productId);
    var qty = parseQty(line.qty);
    if (!p) { info.appendChild(h('span', null, 'Select a product.')); return; }
    if (qty === null) {
      info.appendChild(h('span', { class: 'line-warn' }, 'Quantity must be a whole number from 1 to ' + MAX_QTY + '.'));
      return;
    }
    var cents = parseCents(p.price);
    info.appendChild(h('span', null, qty + ' x ' + fmtMoneyString(p.price)));
    if (qty > p.quantity_in_stock) {
      info.appendChild(h('span', { class: 'line-warn' }, 'Only ' + p.quantity_in_stock + ' in stock'));
    }
    info.appendChild(h('span', { class: 'line-total' }, fmtCents(cents * BigInt(qty))));
  }

  function updatePreview() {
    var totals = [];
    state.lines.forEach(function (line) {
      updateLineInfo(line);
      var p = line.productId === null ? null : findProduct(line.productId);
      var qty = parseQty(line.qty);
      if (p && qty !== null) { totals.push(parseCents(p.price) * BigInt(qty)); }
    });
    var t = computeTotals(totals);
    setText('pv-subtotal', fmtCents(t.subtotal));
    setText('pv-discount-l', t.discountPct > 0n ? 'Discount (' + t.discountPct + '%)' : 'Discount');
    setText('pv-discount', t.discount > 0n ? '-' + fmtCents(t.discount) : fmtCents(0n));
    setText('pv-tax-l', 'Tax (' + rateLabel(state.taxRate) + '%)');
    setText('pv-tax', fmtCents(t.tax));
    setText('pv-total', fmtCents(t.total));
  }

  function lineByKey(key) {
    for (var i = 0; i < state.lines.length; i++) { if (String(state.lines[i].key) === String(key)) { return state.lines[i]; } }
    return null;
  }

  function addToOrder(productId) {
    var existing = state.lines.filter(function (l) { return l.productId === productId; })[0];
    var focusKey;
    if (existing) {
      var q = parseQty(existing.qty) || 0;
      existing.qty = String(Math.min(q + 1, MAX_QTY));
      focusKey = existing.key;
    } else {
      var line = newLine(productId, '1');
      state.lines.push(line);
      focusKey = line.key;
    }
    renderLines(null);
    var qty = $('line-q-' + focusKey);
    if (qty) { qty.scrollIntoView({ block: 'center' }); qty.focus(); qty.select(); }
    var p = findProduct(productId);
    toast((p ? p.sku : 'Product') + ' added to the order.');
  }

  function resetOrder() {
    state.lines = [];
    renderLines(null);
    hideError('order');
    $('order-success').hidden = true;
  }

  function submitOrder(ev) {
    ev.preventDefault();
    hideError('order');
    $('order-success').hidden = true;
    var items = [];
    var problems = [];
    var seen = {};
    state.lines.forEach(function (line, idx) {
      var qty = parseQty(line.qty);
      if (line.productId === null) { problems.push('Line ' + (idx + 1) + ': choose a product.'); return; }
      if (qty === null) { problems.push('Line ' + (idx + 1) + ': quantity must be a whole number from 1 to ' + MAX_QTY + '.'); return; }
      if (seen[line.productId]) { problems.push('Line ' + (idx + 1) + ': product is already on another line.'); return; }
      seen[line.productId] = true;
      items.push({ product_id: line.productId, quantity: qty });
    });
    if (state.lines.length === 0) { problems.push('Add at least one item.'); }
    if (problems.length) {
      showError('order', new ApiError(0, 'validation_error', 'This order cannot be placed yet.', problems));
      return;
    }
    var btn = $('order-submit');
    btn.disabled = true;
    api('POST', '/orders', { line_items: items }).then(function (order) {
      state.lines = [];
      renderLines(null);
      state.lastOrderId = order.id;
      setText('order-success-msg', 'Order #' + order.id + ' confirmed. Total ' + fmtMoneyString(order.total) + '.');
      $('order-success').hidden = false;
      toast('Order #' + order.id + ' placed.');
      return refreshAll();
    }).catch(function (err) {
      showError('order', err);
      if (err.code === 'insufficient_stock' || err.code === 'product_inactive' || err.code === 'product_not_found') {
        refreshAll(); // stock or availability changed; bring the UI up to date
      }
    }).then(function () { btn.disabled = false; });
  }

  // ------------------------------------------------------------------ events
  function debounce(fn, ms) {
    var t = null;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }

  function init() {
    wireDialog($('product-dialog'));
    wireDialog($('stock-dialog'));
    wireDialog($('order-dialog'));

    // filters
    var reload = debounce(loadProducts, 250);
    $('f-q').addEventListener('input', reload);
    $('f-category').addEventListener('change', loadProducts);
    $('f-active').addEventListener('change', loadProducts);
    $('f-low').addEventListener('change', loadProducts);
    $('filters').addEventListener('submit', function (ev) { ev.preventDefault(); loadProducts(); });
    $('clear-filters').addEventListener('click', function () {
      $('filters').reset();
      loadProducts();
      $('f-q').focus();
    });
    $('products-retry').addEventListener('click', loadProducts);
    $('orders-retry').addEventListener('click', loadOrders);
    $('refresh-orders').addEventListener('click', function () { loadOrders().then(function () { toast('Orders refreshed.'); }); });

    // product table actions (event delegation)
    $('products-body').addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-action]');
      if (!btn) { return; }
      var id = parseInt(btn.dataset.id, 10);
      var action = btn.dataset.action;
      var local = findProduct(id);
      function withProduct(fn) {
        // Always edit/adjust against fresh server data.
        api('GET', '/products/' + id).then(fn, function (err) {
          if (local) { fn(local); } else { toast(err.message, true); }
        });
      }
      if (action === 'edit') { withProduct(openProductDialog); }
      else if (action === 'stock') { withProduct(openStockDialog); }
      else if (action === 'add-to-order') {
        if (local) { addToOrder(id); }
        else { loadCatalog().then(function () { addToOrder(id); }); }
      }
    });

    $('orders-body').addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-action="view-order"]');
      if (btn) { openOrderDialog(parseInt(btn.dataset.id, 10)); }
    });

    $('new-product').addEventListener('click', function () { openProductDialog(null); });
    $('product-form').addEventListener('submit', submitProduct);
    $('stock-form').addEventListener('submit', submitStock);
    $('sf-delta').addEventListener('input', previewStock);

    // order builder
    $('add-line').addEventListener('click', function () {
      var line = newLine(null, '1');
      state.lines.push(line);
      renderLines(line.key);
    });
    $('order-clear').addEventListener('click', resetOrder);
    $('order-form').addEventListener('submit', submitOrder);
    $('order-success-view').addEventListener('click', function () {
      if (state.lastOrderId !== null) { openOrderDialog(state.lastOrderId); }
    });
    var lines = $('order-lines');
    lines.addEventListener('change', function (ev) {
      if (!ev.target.classList.contains('line-product')) { return; }
      var line = lineByKey(ev.target.dataset.key);
      if (!line) { return; }
      line.productId = ev.target.value === '' ? null : parseInt(ev.target.value, 10);
      refreshLineSelects();
    });
    lines.addEventListener('input', function (ev) {
      if (!ev.target.classList.contains('line-qty')) { return; }
      var line = lineByKey(ev.target.dataset.key);
      if (!line) { return; }
      line.qty = ev.target.value;
      updatePreview();
    });
    lines.addEventListener('click', function (ev) {
      var btn = ev.target.closest('button[data-action="remove-line"]');
      if (!btn) { return; }
      var key = btn.dataset.key;
      var idx = state.lines.findIndex(function (l) { return String(l.key) === String(key); });
      if (idx === -1) { return; }
      state.lines.splice(idx, 1);
      var next = state.lines[Math.min(idx, state.lines.length - 1)];
      renderLines(next ? next.key : null);
      if (!next) { $('add-line').focus(); }
    });

    updatePreview();
    refreshAll();
  }

  if (typeof BigInt === 'undefined') {
    toast('This browser is too old for the interactive features.', true);
    return;
  }
  init();
})();
