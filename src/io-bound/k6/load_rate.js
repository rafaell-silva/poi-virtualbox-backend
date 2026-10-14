/**
 * k6 — Teste com taxa de chegada controlada (Open Workload Model)
 *
 * Modelo de Carga Aberto (Open Model / Ramping Arrival Rate):
 *   No modelo aberto, o gerador de carga agenda novas iterações/requisições a uma
 *   taxa predeterminada (requisições por segundo), INDEPENDENTEMENTE do tempo de
 *   resposta da aplicação.
 *   Consequência teórica: se a aplicação saturar em memória e aumentar a latência,
 *   as requisições não desaceleram automaticamente; elas se acumulam em fila.
 *   Se o pool de VUs (`maxVUs`) for insuficiente para manter as requisições
 *   em voo (in-flight), o k6 DESCARTA iterações e registra a métrica
 *   `dropped_iterations`.
 *
 * ATENÇÃO CRÍTICA — Rastreabilidade da Taxa Efetiva:
 *   Se `dropped_iterations > 0`, a taxa programada NÃO foi entregue integralmente
 *   à aplicação porque o próprio gerador atingiu seu limite de VUs simultâneos.
 *   Portanto, não interprete `MAX_RATE` programado como a taxa efetivamente
 *   atendida ou entregue sem antes verificar se `dropped_iterations == 0`.
 *
 * Uso:
 *   k6 run k6/load_rate.js
 *   k6 run k6/load_rate.js -e TARGET_URL=http://192.168.56.10:8000 \
 *                          -e SIZE_MIB=8 -e ITERATIONS=1 -e MAX_RATE=10
 */

import http from "k6/http";
import { check } from "k6";
import { Trend, Rate, Counter } from "k6/metrics";

// -------------------------------------------------------------------------- //
// Parâmetros e Validação
// -------------------------------------------------------------------------- //
const rawUrl = __ENV.TARGET_URL || "http://127.0.0.1:8000";
const TARGET_URL = rawUrl.replace(/\/+$/, "");

const rawSize = parseInt(__ENV.SIZE_MIB || __ENV.SIZE_MB || "8", 10);
const SIZE_MIB = !isNaN(rawSize) && rawSize > 0 ? rawSize : 8;

const rawIter = parseInt(__ENV.ITERATIONS || "1", 10);
const ITERATIONS = !isNaN(rawIter) && rawIter > 0 ? rawIter : 1;

const rawRate = parseInt(__ENV.MAX_RATE || "10", 10);
const MAX_RATE = !isNaN(rawRate) && rawRate > 0 ? rawRate : 10;

// Configuração de VUs para o executor de taxa de chegada
// Little's Law: N_VUs ~= Rate * Latência. Para 10 req/s com latência de 2s -> 20 VUs necessárias.
const PRE_ALLOC_VUS = parseInt(__ENV.PRE_ALLOC_VUS || "20", 10);
const MAX_VUS = parseInt(__ENV.MAX_VUS || "50", 10);

const OUT_DIR = (__ENV.OUT_DIR || "data/results").replace(/\/+$/, "");

const WORKLOAD_URL = `${TARGET_URL}/process?size_mb=${SIZE_MIB}&iterations=${ITERATIONS}`;

// -------------------------------------------------------------------------- //
// Métricas Customizadas
// -------------------------------------------------------------------------- //
const appWriteDuration = new Trend("app_write_duration_ms", true);
const appFsyncDuration = new Trend("app_fsync_duration_ms", true);
const appReadDuration = new Trend("app_read_duration_ms", true);
const appWorkDuration = new Trend("app_work_duration_ms", true);
const appTotalDuration = new Trend("app_total_duration_ms", true);

const errorRate = new Rate("error_rate");
const errorTransportRate = new Rate("error_transport_rate");
const errorStatusRate = new Rate("error_status_rate");
const errorBodyRate = new Rate("error_body_rate");
const totalRequests = new Counter("total_requests");

// -------------------------------------------------------------------------- //
// Estágios Proporcionais a MAX_RATE (conservador para 1 vCPU / 1 GiB RAM)
// -------------------------------------------------------------------------- //
const rate25 = Math.max(1, Math.round(MAX_RATE * 0.25));
const rate50 = Math.max(1, Math.round(MAX_RATE * 0.50));
const rate100 = MAX_RATE;

export const options = {
  scenarios: {
    arrival_rate: {
      executor: "ramping-arrival-rate",
      startRate: 0,
      timeUnit: "1s",
      preAllocatedVUs: PRE_ALLOC_VUS,
      maxVUs: MAX_VUS,
      stages: [
        { duration: "20s", target: rate25 },   // 25% da taxa máxima
        { duration: "20s", target: rate25 },   // patamar 25%
        { duration: "20s", target: rate50 },   // rampa para 50%
        { duration: "20s", target: rate50 },   // patamar 50%
        { duration: "25s", target: rate100 },  // rampa para 100%
        { duration: "25s", target: rate100 },  // patamar 100%
        { duration: "15s", target: 0 },        // desaceleração
      ],
    },
  },
  thresholds: {
    error_rate: ["rate<0.10"],
    http_req_failed: ["rate<0.10"],
    http_req_duration: ["p(95)<8000"],
  },
};

// -------------------------------------------------------------------------- //
// Cenário Principal (Open Model: sem sleep!)
// -------------------------------------------------------------------------- //
export default function () {
  const res = http.get(WORKLOAD_URL, {
    timeout: "30s",
    headers: {
      "Accept": "application/json",
      "Cache-Control": "no-cache",
    },
  });

  totalRequests.add(1);

  let isTransportError = res.status === 0;
  let isStatusError = res.status !== 200 && !isTransportError;
  let isBodyError = false;

  if (res.status === 200) {
    try {
      const data = JSON.parse(res.body);
      if (data.status === "ok" && data.timing) {
        if (typeof data.timing.write_ms === "number") {
          appWriteDuration.add(data.timing.write_ms);
        }
        if (typeof data.timing.fsync_ms === "number") {
          appFsyncDuration.add(data.timing.fsync_ms);
        }
        if (typeof data.timing.read_ms === "number") {
          appReadDuration.add(data.timing.read_ms);
        }
        if (typeof data.timing.work_ms === "number") {
          appWorkDuration.add(data.timing.work_ms);
        }
        if (typeof data.timing.app_total_ms === "number") {
          appTotalDuration.add(data.timing.app_total_ms);
        }
      } else {
        isBodyError = true;
      }
    } catch {
      isBodyError = true;
    }
  }

  const hasAnyError = isTransportError || isStatusError || isBodyError;

  errorTransportRate.add(isTransportError);
  errorStatusRate.add(isStatusError);
  errorBodyRate.add(isBodyError);
  errorRate.add(hasAnyError);

  check(res, {
    "status 200": (r) => r.status === 200,
    "valid body": () => !isBodyError,
  });
}

// -------------------------------------------------------------------------- //
// Exportação dos Resultados Agregados em CSV
// -------------------------------------------------------------------------- //
export function handleSummary(data) {
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const filename = `${OUT_DIR}/k6_rate_${SIZE_MIB}mib_${ITERATIONS}iter_${ts}.csv`;

  const metrics = data.metrics;

  function safe(path, key = "value") {
    try {
      const parts = path.split(".");
      let obj = metrics;
      for (const p of parts) {
        if (obj === undefined || obj === null) return "NA";
        obj = obj[p];
      }
      if (obj === undefined || obj === null) return "NA";
      if (typeof obj === "number" || typeof obj === "string") return String(obj);
      if (obj.values && obj.values[key] !== undefined) return String(obj.values[key]);
      if (obj[key] !== undefined) return String(obj[key]);
      if (obj.values && typeof obj.values === "object") {
        const v = obj.values.value ?? obj.values.count ?? obj.values.rate ?? obj.values.avg;
        if (v !== undefined) return String(v);
      }
      return "NA";
    } catch {
      return "NA";
    }
  }

  const rows = [
    "metric,value",
    `test_run_id,k6_rate_${SIZE_MIB}mib_${ts}`,
    `timestamp_utc,${new Date().toISOString()}`,
    `workload_model,open_arrival_rate`,
    `target_url,${TARGET_URL}`,
    `size_mib,${SIZE_MIB}`,
    `iterations,${ITERATIONS}`,
    `target_max_rate_rps,${MAX_RATE}`,
    `pre_allocated_vus,${PRE_ALLOC_VUS}`,
    `max_vus,${MAX_VUS}`,
    `total_requests,${safe("total_requests", "count")}`,
    `http_reqs_per_second,${safe("http_reqs.values", "rate")}`,
    `dropped_iterations,${safe("dropped_iterations", "count")}`,
    `http_req_duration_avg_ms,${safe("http_req_duration.values", "avg")}`,
    `http_req_duration_med_ms,${safe("http_req_duration.values", "med")}`,
    `http_req_duration_p90_ms,${safe("http_req_duration.values", "p(90)")}`,
    `http_req_duration_p95_ms,${safe("http_req_duration.values", "p(95)")}`,
    `http_req_duration_max_ms,${safe("http_req_duration.values", "max")}`,
    `app_write_duration_avg_ms,${safe("app_write_duration_ms.values", "avg")}`,
    `app_fsync_duration_avg_ms,${safe("app_fsync_duration_ms.values", "avg")}`,
    `app_read_duration_avg_ms,${safe("app_read_duration_ms.values", "avg")}`,
    `app_work_duration_avg_ms,${safe("app_work_duration_ms.values", "avg")}`,
    `app_work_duration_p95_ms,${safe("app_work_duration_ms.values", "p(95)")}`,
    `app_total_duration_avg_ms,${safe("app_total_duration_ms.values", "avg")}`,
    `app_total_duration_p95_ms,${safe("app_total_duration_ms.values", "p(95)")}`,
    `error_rate,${safe("error_rate.values", "rate")}`,
    `error_transport_rate,${safe("error_transport_rate.values", "rate")}`,
    `error_status_rate,${safe("error_status_rate.values", "rate")}`,
    `error_body_rate,${safe("error_body_rate.values", "rate")}`,
    `http_req_failed_rate,${safe("http_req_failed.values", "rate")}`,
  ];

  return {
    [filename]: rows.join("\n") + "\n",
    stdout: textSummary(data),
  };
}

function textSummary(data) {
  const m = data.metrics;
  const lines = ["", "=== Resumo k6 (load_rate - Open Model) ==="];
  for (const [name, metric] of Object.entries(m)) {
    const vals = metric.values || {};
    lines.push(`  ${name}: ${JSON.stringify(vals)}`);
  }
  return lines.join("\n");
}
