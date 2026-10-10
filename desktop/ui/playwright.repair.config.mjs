import { defineConfig } from '@playwright/test';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = join(here, '..', '..');
const backendDir = join(repoRoot, 'desktop', 'backend');
const dataDir = join(here, 'test', '.tmp', 'repair-browser-data');

// R15b repair browser acceptance (D16/D19, V12).
// - outputDir test/.tmp/repair-browser (project-local, git-ignored)
// - JSON reporter (report.json) + list, failure trace retained
// - 60s global timeout; per-test 30s (load 60s)
// - webServer starts the isolated repair harness in acceptance mode
//   (real producers + raw HTTP fixtures) on 127.0.0.1:46409 with a
//   project-local data_dir; Playwright stops it after the run.
// - Browsers live under PLAYWRIGHT_BROWSERS_PATH (project-local
//   desktop/ui/test/.tmp/ms-playwright); offline install failure is
//   reported as UNVERIFIED, never faked via Node status tests.
export default defineConfig({
  testDir: './e2e',
  testMatch: /repair\.spec\.js/,
  outputDir: './test/.tmp/repair-browser',
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [
    ['json', { outputFile: './test/.tmp/repair-browser/report.json' }],
    ['list'],
  ],
  use: {
    baseURL: 'http://127.0.0.1:46409',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  webServer: {
    command: `"${join(backendDir, '.venv', 'bin', 'python')}" "${join(backendDir, 'tests', 'repair_server.py')}" --acceptance --data-dir "${dataDir}" --log-level warning`,
    url: 'http://127.0.0.1:46409/test/harness/state',
    reuseExistingServer: false,
    timeout: 60_000,
    stdout: 'pipe',
    stderr: 'pipe',
    env: {
      PYTHONPATH: `${join(backendDir, 'src')}:${join(backendDir)}`,
      NO_PROXY: '127.0.0.1,localhost',
      no_proxy: '127.0.0.1,localhost',
    },
  },
  projects: [{ name: 'repair-chromium', use: { browserName: 'chromium' } }],
});
