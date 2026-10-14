// Teste rapido (30s) so para validar que a API responde antes da rampa completa.
import http from 'k6/http';
import { check } from 'k6';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';

export const options = { vus: 2, duration: '30s' };

export default function () {
  const res = http.get(`${BASE_URL}/info`);
  check(res, { 'status 200': (r) => r.status === 200 });
}
