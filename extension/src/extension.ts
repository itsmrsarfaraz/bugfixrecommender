import * as vscode from 'vscode';
import * as http from 'http';
import { ResultsPanel } from './resultsPanel';

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

interface GenerateFixResponse {
  fixed_code: string;
  model: string;
}

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

async function runRecommend(
  context: vscode.ExtensionContext,
  mode: 'selection' | 'file'
): Promise<void> {
  const editor = vscode.window.activeTextEditor;
  if (!editor) {
    vscode.window.showWarningMessage('Bug Fix Recommender: No active editor.');
    return;
  }

  let buggyCode: string;
  if (mode === 'selection') {
    buggyCode = editor.document.getText(editor.selection);
    if (!buggyCode.trim()) {
      buggyCode = editor.document.getText();
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

  const isHealthy = await checkServerHealth(serverUrl);
  if (!isHealthy) {
    const action = await vscode.window.showErrorMessage(
      'Bug Fix Recommender: API server is not running.',
      'How to start',
      'Dismiss'
    );
    if (action === 'How to start') {
      vscode.window.showInformationMessage(
        'Open a terminal and run:\ncd ~/bugfixrecommender && python -m api.server'
      );
    }
    return;
  }

  const statusBar = vscode.window.createStatusBarItem(
    vscode.StatusBarAlignment.Left,
    100
  );
  statusBar.text = '$(loading~spin) Bug Fix Recommender: analysing...';
  statusBar.show();

  try {
    // Run BM25 and CodeT5 in parallel
    const [response, generatedFix] = await Promise.all([
      queryServer(serverUrl, buggyCode, topK),
      generateFix(serverUrl, buggyCode),
    ]);

    if (!response || response.results.length === 0) {
      vscode.window.showInformationMessage(
        'Bug Fix Recommender: No matching fixes found. Try selecting a smaller code block.'
      );
      return;
    }

    ResultsPanel.createOrShow(context.extensionUri, response, buggyCode, generatedFix);

    statusBar.text =
      `$(check) Bug Fix: ${response.results.length} results ` +
      `(${response.query_time_ms.toFixed(0)}ms)`;
    setTimeout(() => statusBar.hide(), 3000);

  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    vscode.window.showErrorMessage(`Bug Fix Recommender: ${msg}`);
  } finally {
    statusBar.dispose();
  }
}

function queryServer(
  serverUrl: string,
  buggyCode: string,
  topK: number
): Promise<RecommendResponse> {
  return new Promise((resolve, reject) => {
    const body = JSON.stringify({ buggy_code: buggyCode, top_k: topK });
    const { hostname, port } = parseUrl(serverUrl);

    const options: http.RequestOptions = {
      hostname, port,
      path: '/recommend',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
      timeout: 15000,
    };

    const req = http.request(options, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk.toString(); });
      res.on('end', () => {
        try {
          if (res.statusCode === 503) {
            reject(new Error('Index not loaded. Run: python main.py --step index'));
            return;
          }
          resolve(JSON.parse(data) as RecommendResponse);
        } catch {
          reject(new Error('Invalid JSON from server'));
        }
      });
    });

    req.on('timeout', () => { req.destroy(); reject(new Error('Request timed out (15s)')); });
    req.on('error', (err: Error & { code?: string }) => {
      if (err.code === 'ECONNREFUSED') {
        reject(new Error('Connection refused. Start server: python -m api.server'));
      } else {
        reject(err);
      }
    });

    req.write(body);
    req.end();
  });
}

function generateFix(
  serverUrl: string,
  buggyCode: string
): Promise<GenerateFixResponse | null> {
  return new Promise((resolve) => {
    const body = JSON.stringify({ buggy_code: buggyCode.slice(0, 2000) });
    const { hostname, port } = parseUrl(serverUrl);

    const options: http.RequestOptions = {
      hostname, port,
      path: '/generate-fix',
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
      },
      timeout: 60000,
    };

    const req = http.request(options, (res) => {
      let data = '';
      res.on('data', (chunk) => { data += chunk.toString(); });
      res.on('end', () => {
        try { resolve(JSON.parse(data) as GenerateFixResponse); }
        catch { resolve(null); }
      });
    });

    req.on('timeout', () => { req.destroy(); resolve(null); });
    req.on('error', () => resolve(null));
    req.write(body);
    req.end();
  });
}

function checkServerHealth(serverUrl: string): Promise<boolean> {
  return new Promise((resolve) => {
    const { hostname, port } = parseUrl(serverUrl);
    const req = http.get(
      { hostname, port, path: '/health', timeout: 2000 },
      (res) => resolve(res.statusCode === 200)
    );
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

function parseUrl(serverUrl: string): { hostname: string; port: number } {
  const withoutProto = serverUrl.replace(/^https?:\/\//, '');
  const [hostPart, portStr] = withoutProto.split(':');
  return {
    hostname: hostPart || '127.0.0.1',
    port: portStr ? parseInt(portStr, 10) : 8000,
  };
}