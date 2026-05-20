"use strict";
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
var __createBinding = (this && this.__createBinding) || (Object.create ? (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    var desc = Object.getOwnPropertyDescriptor(m, k);
    if (!desc || ("get" in desc ? !m.__esModule : desc.writable || desc.configurable)) {
      desc = { enumerable: true, get: function() { return m[k]; } };
    }
    Object.defineProperty(o, k2, desc);
}) : (function(o, m, k, k2) {
    if (k2 === undefined) k2 = k;
    o[k2] = m[k];
}));
var __setModuleDefault = (this && this.__setModuleDefault) || (Object.create ? (function(o, v) {
    Object.defineProperty(o, "default", { enumerable: true, value: v });
}) : function(o, v) {
    o["default"] = v;
});
var __importStar = (this && this.__importStar) || (function () {
    var ownKeys = function(o) {
        ownKeys = Object.getOwnPropertyNames || function (o) {
            var ar = [];
            for (var k in o) if (Object.prototype.hasOwnProperty.call(o, k)) ar[ar.length] = k;
            return ar;
        };
        return ownKeys(o);
    };
    return function (mod) {
        if (mod && mod.__esModule) return mod;
        var result = {};
        if (mod != null) for (var k = ownKeys(mod), i = 0; i < k.length; i++) if (k[i] !== "default") __createBinding(result, mod, k[i]);
        __setModuleDefault(result, mod);
        return result;
    };
})();
Object.defineProperty(exports, "__esModule", { value: true });
exports.activate = activate;
exports.deactivate = deactivate;
const vscode = __importStar(require("vscode"));
const http = __importStar(require("http"));
const resultsPanel_1 = require("./resultsPanel");
// ── Extension activation ───────────────────────────────────────
function activate(context) {
    console.log('Bug Fix Recommender: activated');
    const recommendCmd = vscode.commands.registerCommand('bugfix.recommend', () => runRecommend(context, 'selection'));
    const recommendFileCmd = vscode.commands.registerCommand('bugfix.recommendFile', () => runRecommend(context, 'file'));
    context.subscriptions.push(recommendCmd, recommendFileCmd);
}
function deactivate() { }
// ── Core logic ─────────────────────────────────────────────────
async function runRecommend(context, mode) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('Bug Fix Recommender: No active editor.');
        return;
    }
    // Get code to query
    let buggyCode;
    if (mode === 'selection') {
        buggyCode = editor.document.getText(editor.selection);
        if (!buggyCode.trim()) {
            buggyCode = editor.document.getText(); // fall back to whole file
        }
    }
    else {
        buggyCode = editor.document.getText();
    }
    if (!buggyCode.trim()) {
        vscode.window.showWarningMessage('Bug Fix Recommender: No code to analyse.');
        return;
    }
    const config = vscode.workspace.getConfiguration('bugfixRecommender');
    const serverUrl = config.get('serverUrl', 'http://127.0.0.1:8000');
    const topK = config.get('topK', 5);
    // Quick health check before querying
    const isHealthy = await checkServerHealth(serverUrl);
    if (!isHealthy) {
        const action = await vscode.window.showErrorMessage('Bug Fix Recommender: API server is not running.', 'How to start', 'Dismiss');
        if (action === 'How to start') {
            vscode.window.showInformationMessage('Open a terminal and run:\n' +
                'cd D:\\bugfixrecommender && python -m api.server');
        }
        return;
    }
    // Status bar spinner
    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBar.text = '$(loading~spin) Bug Fix Recommender: searching...';
    statusBar.show();
    try {
        const response = await queryServer(serverUrl, buggyCode, topK);
        if (!response || response.results.length === 0) {
            vscode.window.showInformationMessage('Bug Fix Recommender: No matching fixes found. ' +
                'Try selecting a larger code block.');
            return;
        }
        // Render results in WebView
        resultsPanel_1.ResultsPanel.createOrShow(context.extensionUri, response, buggyCode);
        statusBar.text =
            `$(check) Bug Fix: ${response.results.length} fixes ` +
                `(${response.query_time_ms.toFixed(0)}ms)`;
        setTimeout(() => statusBar.hide(), 3000);
    }
    catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        vscode.window.showErrorMessage(`Bug Fix Recommender: ${msg}`);
    }
    finally {
        statusBar.dispose();
    }
}
// ── HTTP helpers ───────────────────────────────────────────────
function queryServer(serverUrl, buggyCode, topK) {
    return new Promise((resolve, reject) => {
        const body = JSON.stringify({ buggy_code: buggyCode, top_k: topK });
        // Parse host and port from serverUrl string (avoids URL class issues)
        const withoutProto = serverUrl.replace(/^https?:\/\//, '');
        const [hostPart, portStr] = withoutProto.split(':');
        const hostname = hostPart || '127.0.0.1';
        const port = portStr ? parseInt(portStr, 10) : 8000;
        const options = {
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
        const req = http.request(options, (res) => {
            let data = '';
            res.on('data', (chunk) => { data += chunk.toString(); });
            res.on('end', () => {
                try {
                    if (res.statusCode === 503) {
                        reject(new Error('Index not loaded. Run: python main.py --step index'));
                        return;
                    }
                    resolve(JSON.parse(data));
                }
                catch {
                    reject(new Error('Invalid JSON from server'));
                }
            });
        });
        req.on('timeout', () => {
            req.destroy();
            reject(new Error('Request timed out (15s)'));
        });
        req.on('error', (err) => {
            if (err.code === 'ECONNREFUSED') {
                reject(new Error('Connection refused. Start server: python -m api.server'));
            }
            else {
                reject(err);
            }
        });
        req.write(body);
        req.end();
    });
}
function checkServerHealth(serverUrl) {
    return new Promise((resolve) => {
        const withoutProto = serverUrl.replace(/^https?:\/\//, '');
        const [hostPart, portStr] = withoutProto.split(':');
        const hostname = hostPart || '127.0.0.1';
        const port = portStr ? parseInt(portStr, 10) : 8000;
        const req = http.get({ hostname, port, path: '/health', timeout: 2000 }, (res) => resolve(res.statusCode === 200));
        req.on('error', () => resolve(false));
        req.on('timeout', () => { req.destroy(); resolve(false); });
    });
}
//# sourceMappingURL=extension.js.map