// k6 performance test: OPT-IN, never part of the default pipeline. It WRITES data (a dedicated
// high-stock product and a stream of single-unit orders) - run it against a throw-away instance
// or accept that the orders stay in the (non-persistent) store until the next restart.
//
//   k6 run -e BASE_URL=http://localhost:8000 load-tests/performance.js
//   # thresholds and shape straight from config/quality-policy.yaml:
//   k6 run $(python scripts/quality_policy.py args k6) -e BASE_URL=http://localhost:8000 load-tests/performance.js
//
// Environment (all optional):
//   BASE_URL        service root                                   (default http://localhost:8000)
//   VUS             concurrent virtual users                       (default 5   - low on purpose)
//   DURATION        test length, k6 syntax                         (default 30s)
//   P95_MS          http_req_duration p(95) threshold, ms          (default 500 = load_test.p95_ms)
//   ERROR_RATE_MAX  http_req_failed rate threshold                 (default 0.01 = load_test.error_rate_max)
//   ORDER_RATIO     share of iterations that also place an order   (default 0.25)
//   THINK_TIME      seconds a user pauses between iterations       (default 0.2)
//   KEEP_PRODUCT    "1" leaves the k6 product active after the run (default: it is deactivated)
//
// `k6 run --vus N --duration D` overrides VUS/DURATION. The defaults for P95_MS and ERROR_RATE_MAX
// mirror config/quality-policy.yaml; the pipeline passes the policy values explicitly.
import http from 'k6/http';
import { check, group, sleep } from 'k6';
import { Counter, Rate } from 'k6/metrics';

const BASE_URL = (__ENV.BASE_URL || 'http://localhost:8000').replace(/\/+$/, '');
const VUS = parseInt(__ENV.VUS || '5', 10);
const DURATION = __ENV.DURATION || '30s';
const P95_MS = parseFloat(__ENV.P95_MS || '500');
const ERROR_RATE_MAX = parseFloat(__ENV.ERROR_RATE_MAX || '0.01');
const ORDER_RATIO = parseFloat(__ENV.ORDER_RATIO || '0.25');
const THINK_TIME = parseFloat(__ENV.THINK_TIME || '0.2');
const STOCK = 10000000;

const ordersPlaced = new Counter('orders_placed');
const businessErrors = new Rate('business_errors');

export const options = {
  vus: VUS,
  duration: DURATION,
  thresholds: {
    http_req_failed: [`rate<${ERROR_RATE_MAX}`],
    http_req_duration: [`p(95)<${P95_MS}`],
    business_errors: [`rate<${ERROR_RATE_MAX}`],
    checks: [`rate>${1 - ERROR_RATE_MAX}`],
  },
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'max'],
};

const JSON_HEADERS = { headers: { 'Content-Type': 'application/json' } };

function json(res) {
  try {
    return res.json();
  } catch (e) {
    return null;
  }
}

export function setup() {
  // Seed stock is finite, so the test brings its own product (unique SKU per run, <= 32 chars).
  const sku = `K6-${Date.now().toString(36).toUpperCase()}`;
  const res = http.post(
    `${BASE_URL}/api/products`,
    JSON.stringify({
      sku,
      name: 'k6 load-test product',
      description: 'Created by load-tests/performance.js',
      price: '19.99',
      quantity_in_stock: STOCK,
      category: 'LoadTest',
    }),
    { ...JSON_HEADERS, tags: { name: 'setup POST /api/products' } },
  );
  if (res.status !== 201) {
    throw new Error(`setup failed: POST /api/products -> ${res.status} ${res.body}`);
  }
  return { productId: json(res).id, sku };
}

export default function (data) {
  group('browse', () => {
    const list = http.get(`${BASE_URL}/api/products?limit=20`, {
      tags: { name: 'GET /api/products' },
    });
    const ok = check(list, {
      'list: 200': (r) => r.status === 200,
      'list: has items': (r) => ((json(r) || {}).items || []).length > 0,
    });
    businessErrors.add(!ok);

    const one = http.get(`${BASE_URL}/api/products/${data.productId}`, {
      tags: { name: 'GET /api/products/{id}' },
    });
    businessErrors.add(
      !check(one, { 'product: 200': (r) => r.status === 200 }),
    );

    const version = http.get(`${BASE_URL}/api/version`, { tags: { name: 'GET /api/version' } });
    businessErrors.add(!check(version, { 'version: 200': (r) => r.status === 200 }));

    const metrics = http.get(`${BASE_URL}/api/metrics`, { tags: { name: 'GET /api/metrics' } });
    businessErrors.add(!check(metrics, { 'metrics: 200': (r) => r.status === 200 }));
  });

  if (Math.random() < ORDER_RATIO) {
    group('order', () => {
      const res = http.post(
        `${BASE_URL}/api/orders`,
        JSON.stringify({ line_items: [{ product_id: data.productId, quantity: 1 }] }),
        { ...JSON_HEADERS, tags: { name: 'POST /api/orders' } },
      );
      const ok = check(res, {
        'order: 201': (r) => r.status === 201,
        'order: total = subtotal - discount + tax': (r) => {
          const o = json(r);
          if (!o) return false;
          const cents = (v) => Math.round(parseFloat(v) * 100);
          return cents(o.total) === cents(o.subtotal) - cents(o.discount) + cents(o.tax);
        },
      });
      businessErrors.add(!ok);
      if (ok) ordersPlaced.add(1);
    });
  }

  sleep(THINK_TIME);
}

export function teardown(data) {
  if (__ENV.KEEP_PRODUCT === '1') return;
  // Keep the demo catalogue clean: retire the load-test product so it leaves the inventory value.
  http.patch(
    `${BASE_URL}/api/products/${data.productId}`,
    JSON.stringify({ active: false }),
    { ...JSON_HEADERS, tags: { name: 'teardown PATCH /api/products/{id}' } },
  );
}
