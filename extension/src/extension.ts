/**
 * extension.ts — Bug Fix Recommender VS Code Extension
 *
 * WHAT: Sends selected Java code to the local FastAPI server,
 *       displays top-K fix recommendations in a WebView panel.
 *
 * FLOW:
 *   1. User selects buggy code (or opens a Java file)
 *   2. Ctrl+Shift+B  OR  right-click → "Get Recommendations"
 *   3. Extension POSTs to http://127.0.0.1:8000/recommend
 *   4. Results shown in side panel with diff + commit info
 *
 * REQUIREMENTS:
 *   - FastAPI server must be running:
 *     cd D:\bugfixrecommender && python -m api.server
 */

import * as vscode from 'vscode';
import * as http from 'http';
import { ResultsPanel } from './resultsPanel';

// ── Types ──────────────────────────────────────────────────────

interface FixRecommendation {
  rank: number;
  score: number;
  fixed_code: string;
  buggy_code: string;
  commit_message: string;
  repo: string;
  file_path: string;
  pair_id: string;
}

interface RecommendResponse {
  results: FixRecommendation[];
  total_results: number;
  query_time_ms: number;
  pairs_indexed: number;
}

// ── Extension activation ───────────────────────────────────────

export function activate(context: vscode.ExtensionContext): void {
  console.log('Bug Fix Recommender: activated');

  const recommendCmd = vscode.commands.registerCommand(
    'bugfix.recommend',
    () => runRecommend(context, 'selection')
  );

  const recommendFileCmd = vscode.commands.registerCommand(
    'bugfix.recommendFile',
    () => runRecommend(context, 'file')
  );

  context.subscriptions.push(recommendCmd, recommendFileCmd);
}

export function deactivate(): void {}

// ── Core logic ─────────────────────────────────────────────────

async function runRecommend(
  context: vscode.ExtensionContext,
  mode: 'selection' | 'file'
): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage('Bug Fix Recommender: No active editor.');
    return;
  }

  // Get code to query
  let buggyCode: string;
  if (mode === 'selection') {
    buggyCode = editor.document.getText(editor.selection);
    if (!buggyCode.trim()) {
      buggyCode = editor.document.getText(); // fall back to whole file
    }
  } else {
    buggyCode = editor.document.getText();
  }

  if (!buggyCode.trim()) {
    vscode.window.showWarningMessage('Bug Fix Recommender: No code to analyse.');
    return;
  }

  const config = vscode.workspace.getConfiguration('bugfixRecommender');
  const serverUrl = config.get<string>('serverUrl', 'http://127.0.0.1:8000');
  const topK = config.get<number>('topK', 5);

  // Quick health check before querying
  const isHealthy = await checkServerHealth(serverUrl);
  if (!isHealthy) {
    const action = await vscode.window.showErrorMessage(
      'Bug Fix Recommender: API server is not running.',
      'How to start',
      'Dismiss'
    );
    if (action === 'How to start') {
      vscode.window.showInformationMessage(
        'Open a terminal and run:\n' +
        'cd D:\\bugfixrecommender && python -m api.server'
      );
    }
    return;
  }

  // Status bar spinner
  const statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100
  );
  statusBar.text = '$(loading~spin) Bug Fix Recommender: searching...';
  statusBar.show();

  try {
    const response = await queryServer(serverUrl, buggyCode, topK);

    if (!response || response.results.length === 0) {
      vscode.window.showInformationMessage(
        'Bug Fix Recommender: No matching fixes found. ' +
        'Try selecting a larger code block.'
      );
      return;
    }

    // Render results in WebView
    ResultsPanel.createOrShow(context.extensionUri, response, buggyCode);

    statusBar.text =
      `$(check) Bug Fix: ${response.results.length} fixes ` +
      `(${response.query_time_ms.toFixed(0)}ms)`;
    setTimeout(() => statusBar.hide(), 3000);
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    vscode.window.showErrorMessage(`Bug Fix Recommender: ${msg}`);
  } finally {
    statusBar.dispose();
  }
}

// ── HTTP helpers ───────────────────────────────────────────────

function queryServer(
  serverUrl: string,
  buggyCode: string,
  topK: number
): Promise<RecommendResponse> {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ buggy_code: buggyCode, top_k: topK });

    // Parse host and port from serverUrl string (avoids URL class issues)
    const withoutProto = serverUrl.replace(/^https?:\/\//, '');
    const [hostPart, portStr] = withoutProto.split(':');
    const hostname = hostPart || '127.0.0.1';
    const port = portStr ? parseInt(portStr, 10) : 8000;

    const options: http.RequestOptions = {
      hostname,
      port,
      path: '/recommend',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
      timeout: 15000,
    };

    const req = http.request(options, (res: http.IncomingMessage) => {
      let data = '';
      res.on('data', (chunk: Buffer) => { data += chunk.toString(); });
      res.on('end', () => {
        try {
          if (res.statusCode === 503) {
            reject(new Error(
              'Index not loaded. Run: python main.py --step index'
            ));
            return;
          }
          resolve(JSON.parse(data) as RecommendResponse);
        } catch {
          reject(new Error('Invalid JSON from server'));
        }
      });
    });

    req.on('timeout', () => {
      req.destroy();
      reject(new Error('Request timed out (15s)'));
    });

    req.on('error', (err: Error & { code?: string }) => {
      if (err.code === 'ECONNREFUSED') {
        reject(new Error(
          'Connection refused. Start server: python -m api.server'
        ));
      } else {
        reject(err);
      }
    });

    req.write(body);
    req.end();
  });
}

function checkServerHealth(serverUrl: string): Promise<boolean> {
  return new Promise((resolve) => {
    const withoutProto = serverUrl.replace(/^https?:\/\//, '');
    const [hostPart, portStr] = withoutProto.split(':');
    const hostname = hostPart || '127.0.0.1';
    const port = portStr ? parseInt(portStr, 10) : 8000;

    const req = http.get(
      { hostname, port, path: '/health', timeout: 2000 },
      (res: http.IncomingMessage) => resolve(res.statusCode === 200)
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}