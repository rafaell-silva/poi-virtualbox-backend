/**
 * k6 — Teste de carga CPU-bound (Modelo Fechado: rampa de VUs)
 *
 * Cada Usuário Virtual (VU) envia uma requisição, aguarda a resposta e pausa
 * (`sleep`). Como o trabalho por requisição é CPU-bound (hashes SHA-256),
 * aumentar a demanda satura a CPU e a latência sobe; o throughput (RPS) para de
 * crescer (Lei de Little).
 *
 * Parâmetros (via -e / variável de ambiente):
 *   BASE_URL   URL base da aplicação            (default: http://127.0.0.1:8000)
 *   INTENSITY  Iterações de hash por requisição (default: 1000)
 *   CHUNK_KIB  Bloco hasheado por iteração, KiB  (default: 64; >=2 libera o GIL)
 *   OUT_DIR    Diretório para o CSV de resumo    (default: data/results)
 *
 * Uso:
 *   k6 run k6/load-test.js
 *   k6 run k6/load-test.js -e BASE_URL=http://192.168.56.10:8000 -e INTENSITY=4000
 */

import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/+$/, '');
const INTENSITY = parseInt(__ENV.INTENSITY || '1000', 10);
const CHUNK_KIB = parseInt(__ENV.CHUNK_KIB || '64', 10);
const OUT_DIR = (__ENV.OUT_DIR || 'data/results').replace(/\/+$/, '');

const WORKLOAD_URL = `${BASE_URL}/process-image?intensity=${INTENSITY}&chunk_kib=${CHUNK_KIB}`;

// Nível de demanda fixo quando VUS e DURATION são informados por -e (usado pelo
// orquestrador de VM, que executa um nível por vez). Caso contrário, usa a rampa.
const VUS_OVERRIDE = __ENV.VUS ? parseInt(__ENV.VUS, 10) : null;
const DURATION_OVERRIDE = __ENV.DURATION || null;

function buildOptions() {
  const thresholds = {
    http_req_failed: ['rate<0.10'],
    http_req_duration: ['p(95)<5000'],
  };
  if (VUS_OVERRIDE && DURATION_OVERRIDE) {
    return {
      scenarios: {
        fixed: {
          executor: 'constant-vus',
          vus: VUS_OVERRIDE,
          duration: DURATION_OVERRIDE,
          gracefulStop: '5s',
        },
      },
      thresholds,
    };
  }
  return {
    stages: [
      { duration: '15s', target: 1 },   // Linha de base (1 VU)
      { duration: '15s', target: 2 },   // Carga leve
      { duration: '20s', target: 4 },   // Carga intermediária
      { duration: '20s', target: 8 },   // Carga alta
      { duration: '25s', target: 16 },  // Aproximação da saturação
      { duration: '25s', target: 32 },  // Saturação
      { duration: '15s', target: 0 },   // Desaceleração
    ],
    thresholds,
  };
}

export const options = buildOptions();

export default function () {
  const res = http.get(WORKLOAD_URL, {
    timeout: '30s',
    headers: { 'Accept': 'application/json', 'Cache-Control': 'no-cache' },
  });

  check(res, {
    'status 200': (r) => r.status === 200,
    'processed': (r) => r.json('status') === 'processed',
  });

  sleep(0.1);
}

export function handleSummary(data) {
  const ts = new Date().toISOString().replace(/[:.]/g, '-');
  const filename = `${OUT_DIR}/k6_cpu_${INTENSITY}it_${CHUNK_KIB}ki_${ts}.csv`;
  const m = data.metrics;

  function safe(path, key = 'value') {
    try {
      const parts = path.split('.');
      let obj = m;
      for (const p of parts) {
        if (obj === undefined || obj === null) return 'NA';
        obj = obj[p];
      }
      if (obj === undefined || obj === null) return 'NA';
      if (typeof obj === 'number' || typeof obj === 'string') return String(obj);
      if (obj.values && obj.values[key] !== undefined) return String(obj.values[key]);
      if (obj[key] !== undefined) return String(obj[key]);
      if (obj.values && typeof obj.values === 'object') {
        const v = obj.values.value ?? obj.values.count ?? obj.values.rate ?? obj.values.avg;
        if (v !== undefined) return String(v);
      }
      return 'NA';
    } catch {
      return 'NA';
    }
  }

  const rows = [
    'metric,value',
    `test_run_id,k6_cpu_${INTENSITY}it_${CHUNK_KIB}ki_${ts}`,
    `timestamp_utc,${new Date().toISOString()}`,
    `workload_model,closed_vu_ramp`,
    `target_url,${BASE_URL}`,
    `intensity,${INTENSITY}`,
    `chunk_kib,${CHUNK_KIB}`,
    `total_requests,${safe('http_reqs', 'count')}`,
    `http_reqs_per_second,${safe('http_reqs.values', 'rate')}`,
    `vus_max,${safe('vus_max.values', 'max')}`,
    `http_req_duration_avg_ms,${safe('http_req_duration.values', 'avg')}`,
    `http_req_duration_p90_ms,${safe('http_req_duration.values', 'p(90)')}`,
    `http_req_duration_p95_ms,${safe('http_req_duration.values', 'p(95)')}`,
    `http_req_duration_max_ms,${safe('http_req_duration.values', 'max')}`,
    `http_req_failed_rate,${safe('http_req_failed.values', 'rate')}`,
  ];

  return {
    [filename]: rows.join('\n') + '\n',
    stdout: '\n=== Resumo k6 (cpu-bound) ===\n',
  };
}
