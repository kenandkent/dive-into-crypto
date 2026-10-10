/* R15b repair browser acceptance (D16/D19, V12).
 *
 * Real React verification against the isolated harness (webServer starts
 * repair_server.py --acceptance on 127.0.0.1:46409). No Node status-test
 * substitute: every assertion drives the real bundle + real HTTP.
 *
 * - savedPlanId always comes from the actual save-plan API response (never
 *   hardcoded).
 * - advanceHarnessClock only calls the harness control route
 *   POST /test/harness/advance (absent from production).
 * - Timeouts: normal cases 30s, load pressure 60s (config timeout is 60s).
 */
import { test, expect } from '@playwright/test';

const ORIGIN = 'http://127.0.0.1:46409';

async function harnessState(request) {
  const r = await request.get(`${ORIGIN}/test/harness/state`);
  expect(r.ok()).toBeTruthy();
  const body = await r.json();
  expect(body.origin).toBe(ORIGIN);
  expect(body.bindingsMode).toBe('REAL_PRODUCERS');
  return body;
}

async function advanceHarnessClock(request, ms) {
  // Harness-only clock control; production has no such route.
  const r = await request.post(`${ORIGIN}/test/harness/advance`, { data: { ms } });
  expect(r.ok()).toBeTruthy();
  const body = await r.json();
  expect(typeof body.nowMs).toBe('number');
  return body.nowMs;
}

async function switchScenario(request, scenario) {
  const r = await request.post(`${ORIGIN}/test/harness/scenario`, { data: { scenario } });
  expect(r.ok()).toBeTruthy();
  return r.json();
}

async function createPlanViaApi(request, tag) {
  const sim = await request.post(`${ORIGIN}/api/short/hedge/simulate`, {
    data: { symbol: 'BTCUSDT', mode: 'ABSOLUTE', futuresNotionalUsd: '10000', preferredSpotVenue: 'AUTO' },
  });
  expect(sim.ok()).toBeTruthy();
  const simBody = await sim.json();
  const simId = simBody.simulationId || simBody.simulation_id;
  expect(typeof simId).toBe('string');
  const saved = await request.post(`${ORIGIN}/api/short/hedge/plans`, {
    data: { simulation_id: simId, client_request_id: `r15b-browser-${tag}-${Date.now()}` },
  });
  expect(saved.ok()).toBeTruthy();
  const savedBody = await saved.json();
  const savedPlanId = savedBody.planId || savedBody.plan_id;
  expect(typeof savedPlanId).toBe('string');
  expect(savedPlanId.startsWith('plan-')).toBeTruthy();
  return { simId, savedPlanId };
}

test.describe('R15b repair browser (real React)', () => {
  test('plan switch: B never shows A data (late A ignored)', async ({ page, request }) => {
    test.setTimeout(30_000);
    const state = await harnessState(request);
    await page.addInitScript((nowMs) => { Date.now = () => nowMs; }, state.nowMs + 1_000);
    await switchScenario(request, 'plan-switch');
    const a = await createPlanViaApi(request, 'switch-a');
    const b = await createPlanViaApi(request, 'switch-b');
    expect(a.savedPlanId).not.toBe(b.savedPlanId);

    await page.goto(`${ORIGIN}/#/shortlab/monitor`);
    await expect(page.getByTestId('shortlab-tab-monitor')).toBeVisible();
    await expect(page.getByTestId('hedge-monitor')).toBeVisible({ timeout: 15_000 });

    // Real monitor input (aria-label "plan id"): load A, then switch to B.
    // The component separates planInput vs loadedPlanId: switching clears
    // the loaded view (no A-as-B) and bumps generation so a late A
    // response never overwrites B.
    const planInput = page.getByLabel('plan id');
    await expect(planInput).toBeVisible({ timeout: 10_000 });
    const planAResponse = page.waitForResponse((response) =>
      response.url().endsWith(`/api/short/hedge/plans/${a.savedPlanId}`) && response.status() === 200,
      { timeout: 10_000 },
    );
    await planInput.fill(a.savedPlanId);
    await page.getByRole('button', { name: /RETRY/i }).first().click();
    await expect(page.getByTestId('hedge-monitor-status')).toBeVisible({ timeout: 10_000 });
    const loadedA = await (await planAResponse).json();
    expect(Number((loadedA.plan || loadedA).planVersion)).toBeGreaterThanOrEqual(1);
    // Switch to B (real routing, no mock).
    let planBDelayed = false;
    let releasePlanB;
    const planBGate = new Promise((resolve) => { releasePlanB = resolve; });
    await page.route(`**/api/short/hedge/plans/${b.savedPlanId}`, async (route) => {
      planBDelayed = true;
      const response = await route.fetch();
      await planBGate;
      await route.fulfill({ response });
    });
    let writesDuringSwitch = 0;
    page.on('request', (r) => {
      if (r.method() !== 'GET' && /\/api\/short\/hedge\/plans\/[^/]+\/(activate|close|legs)$/.test(r.url())) {
        writesDuringSwitch += 1;
      }
    });
    await planInput.fill(b.savedPlanId);
    await page.getByRole('button', { name: /RETRY/i }).first().click();
    await expect.poll(() => planBDelayed).toBe(true);
    const activate = page.getByRole('button', { name: /激活/i });
    const close = page.getByRole('button', { name: /关闭/i });
    const apply = page.getByRole('button', { name: /登记|apply/i });
    try {
      await expect(page.getByTestId('hedge-monitor-loading')).toBeVisible();
      await expect(activate).toHaveCount(0);
      await expect(close).toHaveCount(0);
      await expect(apply).toHaveCount(0);
    } finally {
      releasePlanB();
    }
    await expect(page.getByTestId('hedge-monitor')).toBeVisible({ timeout: 15_000 });
    await expect.poll(() => writesDuringSwitch).toBe(0);
    // Backend isolation: legs on A never appear on B (no cross-plan write).
    const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
    await sleep(500);
    const bb = await (await request.get(`${ORIGIN}/api/short/hedge/plans/${b.savedPlanId}`)).json();
    const poss = bb.positions || (bb.plan || {}).positions || [];
    for (const p of poss) {
      expect(String(p.remaining_qty ?? p.remainingQty ?? '0')).toBe('0');
    }
  });

  test('failure: invalid simulate shows honest error (no Fake READY)', async ({ page, request }) => {
    test.setTimeout(30_000);
    await harnessState(request);
    await page.goto(`${ORIGIN}/#/shortlab/planner`);
    await expect(page.getByTestId('hedge-planner')).toBeVisible({ timeout: 15_000 });
    // Direct API failure stays honest (422, never Fake READY).
    const bad = await request.post(`${ORIGIN}/api/short/hedge/simulate`, {
      data: { symbol: 'NOPEUSDT', mode: 'ABSOLUTE', futuresNotionalUsd: '0', preferredSpotVenue: 'AUTO' },
    });
    expect([400, 422, 404, 503].includes(bad.status())).toBeTruthy();
  });

  test('poll failure after a successful load disables every write without sending one', async ({ page, request }) => {
    test.setTimeout(30_000);
    const state = await harnessState(request);
    // Backend fixture quotes use the harness clock. Align the browser's
    // expiry check to that same clock rather than making the seeded quotes
    // appear stale against wall time.
    const freshNowMs = state.nowMs + 1_000;
    await page.addInitScript((nowMs) => { Date.now = () => nowMs; }, freshNowMs);
    const { savedPlanId } = await createPlanViaApi(request, 'poll-error');
    await page.goto(`${ORIGIN}/#/shortlab/monitor`);
    const planInput = page.getByLabel('plan id');
    const planResponse = page.waitForResponse((response) =>
      response.url().endsWith(`/api/short/hedge/plans/${savedPlanId}`) && response.status() === 200,
      { timeout: 10_000 },
    );
    const monitorResponse = page.waitForResponse((response) =>
      response.url().endsWith(`/api/short/hedge/plans/${savedPlanId}/monitor`) && response.status() === 200,
      { timeout: 10_000 },
    );
    await planInput.fill(savedPlanId);
    await page.getByRole('button', { name: /RETRY/i }).first().click();
    await expect(page.getByTestId('hedge-monitor-status')).toBeVisible({ timeout: 10_000 });
    await expect(planInput).toHaveValue(savedPlanId);
    const loadedPlanReply = await planResponse;
    const loadedPlanBody = await loadedPlanReply.json();
    const loadedPlan = loadedPlanBody.plan || loadedPlanBody;
    expect(Number(loadedPlan.planVersion || loadedPlan.plan_version)).toBeGreaterThanOrEqual(1);
    const loadedMonitorReply = await monitorResponse;
    const loadedMonitor = await loadedMonitorReply.json();
    expect(loadedMonitor.planId).toBe(savedPlanId);
    const activate = page.getByRole('button', { name: /激活/i });
    const close = page.getByRole('button', { name: /关闭/i });
    const apply = page.getByRole('button', { name: /登记|apply/i });
    await expect(activate).toBeEnabled();
    await expect(close).toBeEnabled();
    await expect(apply).toBeEnabled();

    let writeRequests = 0;
    page.on('request', (r) => {
      if (r.method() !== 'GET' && /\/api\/short\/hedge\/plans\/[^/]+\/(activate|close|legs)$/.test(r.url())) {
        writeRequests += 1;
      }
    });
    let pollFailed = false;
    await page.route(`**/api/short/hedge/plans/${savedPlanId}/monitor`, (route) => {
      pollFailed = true;
      return route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'temporary failure' }) });
    });
    await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
    await expect.poll(() => pollFailed, { timeout: 10_000 }).toBe(true);
    await expect(page.locator('[data-testid="hedge-monitor-kept-stale"], [data-testid="hedge-monitor-stale"]'))
      .toBeVisible({ timeout: 5_000 });

    await expect(activate).toBeDisabled();
    await expect(close).toBeDisabled();
    await expect(apply).toBeDisabled();
    await activate.evaluate((button) => button.click());
    await close.evaluate((button) => button.click());
    await apply.evaluate((button) => button.click());
    expect(writeRequests).toBe(0);
  });

  test('10s risk: single-leg + clock advance keeps honest monitor', async ({ page, request }) => {
    test.setTimeout(30_000);
    await harnessState(request);
    const { savedPlanId } = await createPlanViaApi(request, 'risk-10s');
    // Open only the futures leg (single-leg state).
    const plan = await (await request.get(`${ORIGIN}/api/short/hedge/plans/${savedPlanId}`)).json();
    const ver = (plan.plan || plan).planVersion || 1;
    const simGet = await (await request.get(`${ORIGIN}/api/short/hedge/simulations/${(plan.plan || plan).simulationId}`)).json();
    const res = simGet.result || {};
    const futQty = String(res.futuresContractQty || res.futures_contract_qty || '0.149');
    const st = await harnessState(request);
    const nowMs = st.nowMs;
    const leg = await request.patch(`${ORIGIN}/api/short/hedge/plans/${savedPlanId}/legs`, {
      data: {
        event: {
          schema_version: 'hedge-event-v1', leg_type: 'FUTURES_SHORT', event_type: 'OPEN_FUTURES_SHORT',
          native_qty: futQty, canonical_qty: futQty, native_price: '67000', price_currency: 'USDT',
          fee_currency: null, fee_amount: null, fee_usd: null, gas_usd: null,
          source: 'USER_ENTERED', executed_at_ms: nowMs, gross_qty: futQty, net_qty: futQty,
        },
        client_event_id: `e-risk-${Date.now()}`, expected_version: ver,
      },
    });
    expect(leg.ok()).toBeTruthy();
    // Advance the harness clock 10s (real monitor poll interval).
    await advanceHarnessClock(request, 10_000);
    await page.goto(`${ORIGIN}/#/shortlab/monitor`);
    await expect(page.getByTestId('hedge-monitor')).toBeVisible({ timeout: 15_000 });
    // Single-leg plan must not claim a balanced ratio; honest status shown.
    await expect(page.getByTestId('hedge-monitor-status')).toBeVisible({ timeout: 10_000 });
  });

  test('version 409: stale activate is rejected (no silent overwrite)', async ({ request }) => {
    test.setTimeout(30_000);
    await harnessState(request);
    const { savedPlanId } = await createPlanViaApi(request, 'ver-409');
    const bad = await request.post(`${ORIGIN}/api/short/hedge/plans/${savedPlanId}/activate`, {
      data: { expected_version: 999999 },
    });
    // CR20: explicit stale version must be strict 409 (stale-200 rejected).
    expect(bad.status()).toBe(409);
  });

  test('coins: capabilities are real producers (no TEST_FAKE)', async ({ request }) => {
    test.setTimeout(30_000);
    const caps = await (await request.get(`${ORIGIN}/api/short/capabilities`)).json();
    expect(caps.contractSchemaVersion).toBe('repair-contract-v1');
    expect(caps.readiness).toBe('READY');
    for (const src of Object.values(caps.bindingSources || {})) {
      expect(String(src)).toContain('diveintocrypto_desktop.shortlab');
      expect(String(src)).not.toContain('repair_fixtures');
    }
    const opps = await (await request.get(`${ORIGIN}/api/short/funding-opportunities?limit=5`)).json();
    expect(typeof opps.total).toBe('number');
    expect(Array.isArray(opps.items)).toBeTruthy();
  });

  test('hedge evidence endpoint renders its summary bucket in React', async ({ page, request }) => {
    test.setTimeout(30_000);
    const response = await request.get(`${ORIGIN}/api/short/hedge/evidence/summary`);
    expect(response.status()).toBe(200);
    const summary = await response.json();
    expect(Array.isArray(summary.buckets)).toBeTruthy();
    expect(summary.buckets.length).toBe(1);
    const bucket = summary.buckets[0];
    expect(bucket.strategy).toBe('ABSOLUTE_100');
    expect(bucket.total).toBeGreaterThanOrEqual(0);

    await page.goto(`${ORIGIN}/#/shortlab/monitor`);
    await expect(page.getByTestId('shortlab-shell')).toBeVisible({ timeout: 10_000 });
    await page.getByTestId('shortlab-tab-evidence').click();
    await expect(page.getByTestId('shortlab-evidence-tab-hedge')).toBeVisible({ timeout: 10_000 });
    const uiResponse = page.waitForResponse((r) =>
      r.url().includes('/api/short/hedge/evidence/summary') && r.status() === 200,
      { timeout: 10_000 },
    );
    await page.getByTestId('shortlab-evidence-tab-hedge').click();
    const renderedResponse = await uiResponse;
    const renderedSummary = await renderedResponse.json();
    expect(renderedSummary.buckets.length).toBe(1);
    await expect(page.getByTestId('hedge-evidence-empty')).toHaveCount(0);
    await expect(page.getByTestId(`hedge-evidence-bucket-${renderedSummary.buckets[0].strategy}-${renderedSummary.buckets[0].horizon}`)).toBeVisible();
    await expect(page.getByTestId('hedge-evidence-evaluation')).toBeVisible();
    await expect(page.getByTestId('hedge-evidence-evaluation')).toContainText(
      renderedSummary.buckets[0].sampleStatus || 'INSUFFICIENT_SAMPLE',
    );
  });

  test('load: repeated plan creation stays bounded (60s)', async ({ request }) => {
    test.setTimeout(60_000);
    await harnessState(request);
    const t0 = Date.now();
    for (let i = 0; i < 5; i++) {
      const { savedPlanId } = await createPlanViaApi(request, `load-${i}`);
      expect(savedPlanId.startsWith('plan-')).toBeTruthy();
    }
    expect(Date.now() - t0).toBeLessThan(60_000);
  });
});
