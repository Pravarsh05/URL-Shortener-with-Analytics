import http from 'k6/http';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

// Custom Metrics
const cacheHits = new Counter('cache_hits');
const cacheMisses = new Counter('cache_misses');
const redirectLatency = new Trend('redirect_duration_ms');
const errorRate = new Rate('errors');

// Benchmarking Thresholds: Verify p99 < 50ms on warm cache
export const options = {
  scenarios: {
    // 1. Cold Cache / Mixed Workload Warm-up
    warmup: {
      executor: 'constant-vus',
      vus: 10,
      duration: '15s',
      startTime: '0s',
    },
    // 2. High-Throughput Warm Cache Redirect Spike
    high_concurrency_redirects: {
      executor: 'ramping-vus',
      startVUs: 20,
      stages: [
        { duration: '20s', target: 200 }, // Ramp-up to 200 VUs
        { duration: '30s', target: 200 }, // Sustain peak load
        { duration: '10s', target: 0 },   // Cool down
      ],
      startTime: '15s',
    },
  },
  thresholds: {
    // Non-functional requirement: p99 under 50ms on cache hit
    'redirect_duration_ms{cache:hit}': ['p(90)<20', 'p(95)<35', 'p(99)<50'],
    'errors': ['rate<0.01'], // Less than 1% errors
  },
};

const BASE_URL = __ENV.TARGET_URL || 'http://localhost:8000';
let testCodes = ['demo1', 'bench_alpha', 'bench_beta'];

export function setup() {
  // Pre-seed test links before running load test
  const codes = [];
  for (let i = 0; i < 5; i++) {
    const alias = `bench_${Date.now()}_${i}`;
    const payload = JSON.stringify({
      url: `https://example.com/target-benchmark-page-${i}`,
      alias: alias,
    });
    const params = {
      headers: { 'Content-Type': 'application/json' },
    };
    const res = http.post(`${BASE_URL}/api/v1/links`, payload, params);
    if (res.status === 201) {
      const data = JSON.parse(res.body);
      codes.push(data.code);
    }
  }
  return { codes: codes.length > 0 ? codes : ['demo1'] };
}

export default function (data) {
  const codes = data.codes;
  const code = codes[Math.floor(Math.random() * codes.length)];

  // Request short URL redirect without following 302 automatically
  // (so we measure redirect latency directly)
  const res = http.get(`${BASE_URL}/${code}`, {
    redirects: 0,
    headers: {
      'User-Agent': 'k6-load-tester/1.0 (X11; Linux x86_64)',
      'X-Forwarded-For': `198.51.100.${Math.floor(Math.random() * 250) + 1}`,
    },
  });

  const isSuccess = check(res, {
    'is status 302 Found': (r) => r.status === 302,
    'has Location header': (r) => r.headers['Location'] !== undefined,
  });

  if (!isSuccess) {
    errorRate.add(1);
  } else {
    errorRate.add(0);
  }

  const cacheHeader = res.headers['X-Cache'] || 'MISS';
  const isHit = cacheHeader === 'HIT';

  if (isHit) {
    cacheHits.add(1);
    redirectLatency.add(res.timings.duration, { cache: 'hit' });
  } else {
    cacheMisses.add(1);
    redirectLatency.add(res.timings.duration, { cache: 'miss' });
  }

  // Small pacing
  sleep(0.01);
}
