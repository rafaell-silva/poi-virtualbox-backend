/**
 * k6 — Teste de carga com rampa de usuários virtuais (Closed Workload Model)
 *
 * Modelo de Carga Fechado (Closed Model):
 *   No modelo fechado, cada Usuário Virtual (VU) executa requisições de forma
 *   sequencial: envia uma requisição, aguarda a resposta da aplicação, aguarda
 *   a pausa configurada (`sleep(0.1)`) e então inicia a próxima iteração.
 *   Consequência teórica: a taxa de requisições por segundo (RPS) NÃO é fixa;
 *   ela varia inversamente à latência da aplicação (Little's Law). Se a memória
 *   saturar e a latência subir, o throughput gerado diminui naturalmente.
 *
 * Coleta de Métricas:
 *   - Latência HTTP do Cliente: registrada pela métrica nativa `http_req_duration`.
 *   - Tempos Internos da Aplicação: extraídos do corpo JSON da resposta:
 *       * `app_work_duration_ms`: tempo gasto em escrita/leitura nos buffers.
 *       * `app_total_duration_ms`: tempo total de processamento interno da requisição.
 *   - Falhas discriminadas:
 *       * `error_transport`: falha de conexão de rede ou timeout (status 0).
 *       * `error_status`: resposta com status HTTP diferente de 200 (ex.: 503 por esgotamento de memória).
 *       * `error_body`: status 200 mas corpo corrompido ou status != ok.
 *       * `error_rate`: taxa agregada de qualquer falha.
 *
 * Exportação de Séries Temporais Contínuas (Timeline):
 *   Por padrão, `handleSummary()` exporta apenas estatísticas agregadas da execução.
 *   Para exportar todas as amostras temporais e correlacionar com CPU/RAM da VM:
 *     k6 run --out json=data/results/k6_timeline.json k6/load_ramp.js
 *
 * Uso:
 *   k6 run k6/load_ramp.js
 *   k6 run k6/load_ramp.js -e TARGET_URL=http://192.168.56.10:8000 \
 *                          -e SIZE_MIB=8 -e ITERATIONS=1 -e OUT_DIR=data/results
 */

import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate, Counter } from "k6/metrics";

// -------------------------------------------------------------------------- //
// Parâmetros e Validação
// -------------------------------------------------------------------------- //
const rawUrl = __ENV.TARGET_URL || "http://127.0.0.1:8000";
const TARGET_URL = rawUrl.replace(/\/+$/, ""); // normaliza removendo barras finais

const rawSize = parseInt(__ENV.SIZE_MIB || __ENV.SIZE_MB || "8", 10);
const SIZE_MIB = !isNaN(rawSize) && rawSize > 0 ? rawSize : 8;

const rawIter = parseInt(__ENV.ITERATIONS || "1", 10);
const ITERATIONS = !isNaN(rawIter) && rawIter > 0 ? rawIter : 1;

const OUT_DIR = (__ENV.OUT_DIR || "data/results").replace(/\/+$/, "");

const WORKLOAD_URL = `${TARGET_URL}/process?size_mb=${SIZE_MIB}&iterations=${ITERATIONS}`;

// -------------------------------------------------------------------------- //
// Métricas Customizadas
// -------------------------------------------------------------------------- //
// Métricas discriminadas da aplicação (extraídas da resposta JSON)
const appWriteDuration = new Trend("app_write_duration_ms", true);
const appFsyncDuration = new Trend("app_fsync_duration_ms", true);
const appReadDuration = new Trend("app_read_duration_ms", true);
const appWorkDuration = new Trend("app_work_duration_ms", true);
const appTotalDuration = new Trend("app_total_duration_ms", true);

// Classificação detalhada de falhas
const errorRate = new Rate("error_rate");
const errorTransportRate = new Rate("error_transport_rate");
const errorStatusRate = new Rate("error_status_rate");
const errorBodyRate = new Rate("error_body_rate");
const totalRequests = new Counter("total_requests");

// -------------------------------------------------------------------------- //
// Configuração de Estágios (Conservadores para ambientes de 1 vCPU / 1 GiB RAM)
// -------------------------------------------------------------------------- //
// Nível de demanda fixo quando VUS e DURATION são informados por -e.
const VUS_OVERRIDE = __ENV.VUS ? parseInt(__ENV.VUS, 10) : null;
const DURATION_OVERRIDE = __ENV.DURATION || null;

function buildOptions() {
  const thresholds = {
    error_rate: ["rate<0.10"],
    http_req_failed: ["rate<0.10"],
    http_req_duration: ["p(95)<5000"],
  };
  if (VUS_OVERRIDE && DURATION_OVERRIDE) {
    return {
      scenarios: {
        fixed: {
          executor: "constant-vus",
          vus: VUS_OVERRIDE,
          duration: DURATION_OVERRIDE,
          gracefulStop: "5s",
        },
      },
      thresholds,
    };
  }
  return {
    stages: [
      { duration: "20s", target: 1 },   // Linha de base (1 VU)
      { duration: "20s", target: 2 },   // Carga leve
      { duration: "20s", target: 4 },   // Carga intermediária
      { duration: "25s", target: 8 },   // Carga alta
      { duration: "25s", target: 16 },  // Aproximação da saturação
      { duration: "25s", target: 32 },  // Saturação
      { duration: "15s", target: 0 },   // Desaceleração
    ],
    thresholds,
  };
}

export const options = buildOptions();

// -------------------------------------------------------------------------- //
// Cenário Principal
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

  // Pausa intencional de 100 ms para controlar o ritmo do modelo fechado
  sleep(0.1);
}

// -------------------------------------------------------------------------- //
// Exportação dos Resultados Agregados em CSV
// -------------------------------------------------------------------------- //
export function handleSummary(data) {
  const ts = new Date().toISOString().replace(/[:.]/g, "-");
  const filename = `${OUT_DIR}/k6_ramp_${SIZE_MIB}mib_${ITERATIONS}iter_${ts}.csv`;

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
    `test_run_id,k6_ramp_${SIZE_MIB}mib_${ts}`,
    `timestamp_utc,${new Date().toISOString()}`,
    `workload_model,closed_vu_ramp`,
    `target_url,${TARGET_URL}`,
    `size_mib,${SIZE_MIB}`,
    `iterations,${ITERATIONS}`,
    `total_requests,${safe("total_requests", "count")}`,
    `http_reqs_per_second,${safe("http_reqs.values", "rate")}`,
    `vus_max,${safe("vus_max.values", "max")}`,
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
  const lines = ["", "=== Resumo k6 (load_ramp - Closed Model) ==="];
  for (const [name, metric] of Object.entries(m)) {
    const vals = metric.values || {};
    lines.push(`  ${name}: ${JSON.stringify(vals)}`);
  }
  return lines.join("\n");
}
