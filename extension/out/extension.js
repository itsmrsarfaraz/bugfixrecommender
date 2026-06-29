"use strict";
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
function activate(context) {
    console.log('Bug Fix Recommender: activated');
    const recommendCmd = vscode.commands.registerCommand('bugfix.recommend', () => runRecommend(context, 'selection'));
    const recommendFileCmd = vscode.commands.registerCommand('bugfix.recommendFile', () => runRecommend(context, 'file'));
    context.subscriptions.push(recommendCmd, recommendFileCmd);
}
function deactivate() { }
async function runRecommend(context, mode) {
    const editor = vscode.window.activeTextEditor;
    if (!editor) {
        vscode.window.showWarningMessage('Bug Fix Recommender: No active editor.');
        return;
    }
    let buggyCode;
    if (mode === 'selection') {
        buggyCode = editor.document.getText(editor.selection);
        if (!buggyCode.trim()) {
            buggyCode = editor.document.getText();
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
    const isHealthy = await checkServerHealth(serverUrl);
    if (!isHealthy) {
        const action = await vscode.window.showErrorMessage('Bug Fix Recommender: API server is not running.', 'How to start', 'Dismiss');
        if (action === 'How to start') {
            vscode.window.showInformationMessage('Open a terminal and run:\ncd ~/bugfixrecommender && python -m api.server');
        }
        return;
    }
    const statusBar = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 100);
    statusBar.text = '$(loading~spin) Bug Fix Recommender: analysing...';
    statusBar.show();
    try {
        // Run BM25 and CodeT5 in parallel
        const [response, generatedFix] = await Promise.all([
            queryServer(serverUrl, buggyCode, topK),
            generateFix(serverUrl, buggyCode),
        ]);
        if (!response || response.results.length === 0) {
            vscode.window.showInformationMessage('Bug Fix Recommender: No matching fixes found. Try selecting a smaller code block.');
            return;
        }
        resultsPanel_1.ResultsPanel.createOrShow(context.extensionUri, response, buggyCode, generatedFix);
        statusBar.text =
            `$(check) Bug Fix: ${response.results.length} results ` +
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
function queryServer(serverUrl, buggyCode, topK) {
    return new Promise((resolve, reject) => {
        const body = JSON.stringify({ buggy_code: buggyCode, top_k: topK });
        const { hostname, port } = parseUrl(serverUrl);
        const options = {
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
                    resolve(JSON.parse(data));
                }
                catch {
                    reject(new Error('Invalid JSON from server'));
                }
            });
        });
        req.on('timeout', () => { req.destroy(); reject(new Error('Request timed out (15s)')); });
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
function generateFix(serverUrl, buggyCode) {
    return new Promise((resolve) => {
        const body = JSON.stringify({ buggy_code: buggyCode.slice(0, 2000) });
        const { hostname, port } = parseUrl(serverUrl);
        const options = {
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
                try {
                    resolve(JSON.parse(data));
                }
                catch {
                    resolve(null);
                }
            });
        });
        req.on('timeout', () => { req.destroy(); resolve(null); });
        req.on('error', () => resolve(null));
        req.write(body);
        req.end();
    });
}
function checkServerHealth(serverUrl) {
    return new Promise((resolve) => {
        const { hostname, port } = parseUrl(serverUrl);
        const req = http.get({ hostname, port, path: '/health', timeout: 2000 }, (res) => resolve(res.statusCode === 200));
        req.on('error', () => resolve(false));
        req.on('timeout', () => { req.destroy(); resolve(false); });
    });
}
function parseUrl(serverUrl) {
    const withoutProto = serverUrl.replace(/^https?:\/\//, '');
    const [hostPart, portStr] = withoutProto.split(':');
    return {
        hostname: hostPart || '127.0.0.1',
        port: portStr ? parseInt(portStr, 10) : 8000,
    };
}
//# sourceMappingURL=extension.js.map