// k6 smoke test: one virtual user, one pass over the read-only endpoints.
// Fast enough to run after every deploy. It places two small orders (a single unit, then one that
// crosses the 5 % discount tier) to prove pricing is right on the deployed build; set SKIP_WRITES=1
// for a strictly read-only run.
//
//   k6 run -e BASE_URL=http://localhost:8000 load-tests/smoke.js
//   k6 run -e BASE_URL=https://stockflow.example.com -e EXPECT_SHA=$GIT_SHA load-tests/smoke.js
//
// Environment:
//   BASE_URL    service root (default http://localhost:8000)
//   EXPECT_SHA  optional; when set, /api/version must report a git_sha starting with it
//   SKIP_WRITES "1" skips the two pricing orders (read-only run)
import http from 'k6/http';
import { check, group } from 'k6';

const BASE_URL = (__ENV.BASE_URL || 'http://localhost:8000').replace(/\/+$/, '');
const EXPECT_SHA = __ENV.EXPECT_SHA || '';
const SKIP_WRITES = __ENV.SKIP_WRITES === '1';

export const options = {
  vus: 1,
  iterations: 1,
  thresholds: {
    // Every check must pass and no request may fail; the latency bound is deliberately loose
    // (a smoke test proves the service works, performance.js proves it is fast).
    checks: ['rate==1'],
    http_req_failed: ['rate==0'],
    http_req_duration: ['p(95)<2000'],
  },
};

function get(path, name) {
  return http.get(`${BASE_URL}${path}`, { tags: { name } });
}

function json(res) {
  try {
    return res.json();
  } catch (e) {
    return null;
  }
}


// ---- pricing helpers: money is handled as integer cents, never as floats --------------------------
function cents(value) {
  const m = /^(\d+)\.(\d{2})$/.exec(String(value));
  return m ? parseInt(m[1], 10) * 100 + parseInt(m[2], 10) : NaN;
}

// Discount tiers (docs/ARCHITECTURE.md 8.1): 10 % from 500.00, 5 % from 100.00; half-up to the cent.
function expectedDiscountCents(subtotalC) {
  const pct = subtotalC >= 50000 ? 10 : subtotalC >= 10000 ? 5 : 0;
  return Math.floor((subtotalC * pct + 50) / 100);
}

// The tax rate is not hard-coded: each order allows an interval of rates (tax is the half-up
// rounding of taxable * rate); the intervals of all orders placed here must overlap.
function rateInterval(order) {
  const taxable = cents(order.subtotal) - cents(order.discount);
  if (taxable <= 0) return null;
  const tax = cents(order.tax);
  return [Math.max(0, (tax - 0.5) / taxable), Math.min(1, (tax + 0.5) / taxable)];
}

// Price invariants of one created order; returns a failure description or '' when all hold.
function pricingProblem(order, product, quantity) {
  const subtotal = cents(order.subtotal);
  const discount = cents(order.discount);
  const tax = cents(order.tax);
  const total = cents(order.total);
  const line = (order.line_items || [])[0] || {};
  if ([subtotal, discount, tax, total].some(Number.isNaN)) return 'amounts are not 2dp strings';
  if (cents(line.unit_price) !== cents(product.price)) return 'unit_price differs from the product price';
  if (line.quantity !== quantity) return 'line quantity differs from the ordered quantity';
  if (cents(line.line_total) !== cents(product.price) * quantity) return 'line_total != unit_price * quantity';
  if (subtotal !== cents(line.line_total)) return 'subtotal != sum of line totals';
  if (discount !== expectedDiscountCents(subtotal)) {
    return `discount ${order.discount} != expected ${expectedDiscountCents(subtotal)} cents for subtotal ${order.subtotal}`;
  }
  if (total !== subtotal - discount + tax) {
    return `total ${order.total} != subtotal ${order.subtotal} - discount ${order.discount} + tax ${order.tax}`;
  }
  return '';
}

// Highest-stock active product that reaches 100.00 <= subtotal < 500.00; null when the catalogue
// has none (the tier order is then skipped rather than failed).
function tierCandidate(items) {
  let best = null;
  for (const p of items) {
    const price = cents(p.price);
    if (!p.active || !(price > 0) || !(p.quantity_in_stock > 0)) continue;
    const quantity = Math.max(1, Math.ceil(10000 / price));
    const subtotal = price * quantity;
    if (quantity > Math.min(p.quantity_in_stock, 10000) || subtotal < 10000 || subtotal >= 50000) continue;
    if (best === null || p.quantity_in_stock > best.product.quantity_in_stock) best = { product: p, quantity };
  }
  return best;
}

function placeOrder(product, quantity, name) {
  return http.post(
    `${BASE_URL}/api/orders`,
    JSON.stringify({ line_items: [{ product_id: product.id, quantity }] }),
    { headers: { 'Content-Type': 'application/json' }, tags: { name } },
  );
}

export default function () {
  group('health', () => {
    const res = get('/health', 'GET /health');
    check(res, {
      'health: 200': (r) => r.status === 200,
      'health: status ok': (r) => (json(r) || {}).status === 'ok',
    });
    const ready = get('/ready', 'GET /ready');
    check(ready, {
      'ready: 200': (r) => r.status === 200,
      'ready: status ready': (r) => (json(r) || {}).status === 'ready',
    });
  });

  group('products', () => {
    const res = get('/api/products?limit=20', 'GET /api/products');
    check(res, {
      'products: 200': (r) => r.status === 200,
      'products: json content type': (r) => (r.headers['Content-Type'] || '').includes('application/json'),
      'products: non-empty list': (r) => {
        const body = json(r);
        return body !== null && Array.isArray(body.items) && body.items.length > 0;
      },
      'products: total >= items': (r) => {
        const body = json(r);
        return body !== null && body.total >= body.items.length;
      },
      'products: money is a 2dp string': (r) => {
        const body = json(r);
        return body !== null && body.items.every((p) => /^\d+\.\d{2}$/.test(p.price));
      },
    });

    const body = json(res);
    if (body && body.items && body.items.length > 0) {
      const id = body.items[0].id;
      const one = get(`/api/products/${id}`, 'GET /api/products/{id}');
      check(one, {
        'product: 200': (r) => r.status === 200,
        'product: same id': (r) => (json(r) || {}).id === id,
      });
    }

    const missing = http.get(`${BASE_URL}/api/products/999999999`, {
      tags: { name: 'GET /api/products/{id} (missing)' },
      responseCallback: http.expectedStatuses(404),
    });
    check(missing, {
      'missing product: 404 error body': (r) =>
        r.status === 404 && ((json(r) || {}).error || {}).code === 'product_not_found',
    });
  });

  group('metrics', () => {
    const res = get('/api/metrics', 'GET /api/metrics');
    check(res, {
      'metrics: 200': (r) => r.status === 200,
      'metrics: has product_count': (r) => typeof (json(r) || {}).product_count === 'number',
    });
  });

  group('version', () => {
    const res = get('/api/version', 'GET /api/version');
    check(res, {
      'version: 200': (r) => r.status === 200,
      'version: name is stockflow': (r) => (json(r) || {}).name === 'stockflow',
      'version: has git_sha': (r) => typeof (json(r) || {}).git_sha === 'string',
    });
    if (EXPECT_SHA) {
      check(res, {
        [`version: git_sha starts with ${EXPECT_SHA.slice(0, 12)}`]: (r) =>
          String((json(r) || {}).git_sha || '').startsWith(EXPECT_SHA),
      });
    }
  });

  if (!SKIP_WRITES) {
    group('pricing', () => {
      const listed = json(get('/api/products?limit=200', 'GET /api/products (all)')) || {};
      const items = (listed.items || []).filter((p) => p.active && p.quantity_in_stock > 0);
      if (items.length === 0) {
        check(null, { 'pricing: an orderable product exists': () => false });
        return;
      }
      const intervals = [];

      // 1) one unit of the first orderable product (normally below every discount tier)
      const single = items[0];
      const r1 = placeOrder(single, 1, 'POST /api/orders (single unit)');
      const o1 = json(r1) || {};
      check(r1, {
        'order: 201': (r) => r.status === 201,
        'order: pricing invariants': () => pricingProblem(o1, single, 1) === '',
      });
      const i1 = rateInterval(o1);
      if (i1) intervals.push(i1);

      // 2) an order that crosses the 5 % discount tier
      const tier = tierCandidate(items);
      if (tier === null) {
        console.warn('pricing: no product can reach a 100.00 - 499.99 subtotal; tier order skipped');
        return;
      }
      const r2 = placeOrder(tier.product, tier.quantity, 'POST /api/orders (5% tier)');
      const o2 = json(r2) || {};
      const problem = r2.status === 201 ? pricingProblem(o2, tier.product, tier.quantity) : 'not created';
      if (problem) console.error(`pricing: tier order ${tier.quantity} x ${tier.product.sku}: ${problem}`);
      check(r2, {
        'tier order: 201': (r) => r.status === 201,
        'tier order: pricing invariants': () => problem === '',
        'tier order: 5% discount applied': () => cents(o2.discount) > 0,
      });
      const i2 = rateInterval(o2);
      if (i2) intervals.push(i2);

      // 3) one tax rate explains every order (catches tax on the undiscounted subtotal)
      check(null, {
        'pricing: tax rate is consistent across orders': () =>
          Math.max(...intervals.map((i) => i[0])) <= Math.min(...intervals.map((i) => i[1])),
      });
    });
  }

  group('dashboard', () => {
    const res = get('/', 'GET /');
    check(res, {
      'dashboard: 200': (r) => r.status === 200,
      'dashboard: html': (r) => (r.headers['Content-Type'] || '').includes('text/html'),
      'dashboard: mentions StockFlow': (r) => r.body.includes('StockFlow'),
    });
  });
}
