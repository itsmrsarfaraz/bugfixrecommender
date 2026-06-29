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
exports.ResultsPanel = void 0;
const vscode = __importStar(require("vscode"));
class ResultsPanel {
    static createOrShow(extensionUri, response, buggyCode, generatedFix) {
        const column = vscode.window.activeTextEditor
            ? vscode.ViewColumn.Beside
            : vscode.ViewColumn.One;
        if (ResultsPanel.currentPanel) {
            ResultsPanel.currentPanel._panel.reveal(column);
            ResultsPanel.currentPanel._update(response, buggyCode, generatedFix);
            return;
        }
        const panel = vscode.window.createWebviewPanel(ResultsPanel.viewType, 'Bug Fix Recommendations', column, {
            enableScripts: true,
            retainContextWhenHidden: true,
            localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'out')],
        });
        ResultsPanel.currentPanel = new ResultsPanel(panel);
        ResultsPanel.currentPanel._update(response, buggyCode, generatedFix);
    }
    constructor(panel) {
        this._disposables = [];
        this._panel = panel;
        this._panel.onDidDispose(() => this._dispose(), null, this._disposables);
        this._panel.webview.onDidReceiveMessage(async (message) => {
            if (message.command === 'applyFix') {
                await this._applyFix(message.code);
            }
        }, null, this._disposables);
    }
    _dispose() {
        ResultsPanel.currentPanel = undefined;
        this._panel.dispose();
        while (this._disposables.length) {
            const d = this._disposables.pop();
            if (d) {
                d.dispose();
            }
        }
    }
    async _applyFix(code) {
        const editor = vscode.window.activeTextEditor;
        if (!editor) {
            vscode.window.showWarningMessage('No active editor to apply fix.');
            return;
        }
        await editor.edit(editBuilder => {
            editBuilder.replace(editor.selection, code);
        });
        vscode.window.showInformationMessage('Bug Fix Recommender: Fix applied!');
    }
    _update(response, buggyCode, generatedFix) {
        this._panel.title = `Bug Fix — ${response.total_results} results`;
        this._panel.webview.html = this._buildHtml(response, buggyCode, generatedFix);
    }
    _buildHtml(response, buggyCode, generatedFix) {
        const maxScore = Math.max(...response.results.map(r => r.score));
        // CodeT5 generated fix banner
        const codet5Banner = generatedFix && generatedFix.fixed_code
            ? `<div class="ai-banner">
           <div class="ai-title">🤖 CodeT5 Generated Fix <span class="ai-badge">${escapeHtml(generatedFix.model)}</span></div>
           <pre class="ai-code">${escapeHtml(generatedFix.fixed_code)}</pre>
           <button class="action-btn btn-primary" onclick="applyAiFix()">⚡ Apply This Fix</button>
         </div>`
            : `<div class="ai-banner ai-unavailable">⚠️ CodeT5 model not available — showing BM25 results only.</div>`;
        const cards = response.results.map((r, index) => {
            const pct = maxScore > 0 ? Math.round((r.score / maxScore) * 100) : 0;
            const repoUrl = `https://github.com/${r.repo}`;
            const commitEscaped = escapeHtml(r.commit_message);
            const fileEscaped = escapeHtml(r.file_path);
            const diffLines = runLcsDiff(r.buggy_code, r.fixed_code);
            const diffHtml = buildInlineDiff(diffLines);
            const fullFixEscaped = escapeHtml(r.fixed_code.length > 800
                ? r.fixed_code.slice(0, 800) + '\n// ... (truncated)'
                : r.fixed_code);
            return `
        <div class="card">
          <div class="card-header">
            <span class="rank">#${r.rank}</span>
            <div class="score-bar-wrap"><div class="score-bar" style="width:${pct}%"></div></div>
            <span class="score-label">Score ${r.score.toFixed(2)}</span>
          </div>

          <div class="diagnosis">
            <span class="diagnosis-label">🐛 Similar bug found:</span>
            <span class="diagnosis-text">${commitEscaped}</span>
          </div>

          <div class="diff-section">
            <span class="section-label">📍 What changed (red = removed, green = added):</span>
            <div class="diff-block">${diffHtml}</div>
          </div>

          <details class="full-fix">
            <summary class="section-label">📋 Full fixed code from this repo (click to expand)</summary>
            <pre class="code-block"><code>${fullFixEscaped}</code></pre>
          </details>

          <div class="card-actions">
            <button class="action-btn btn-secondary" onclick="applyBm25Fix(${index})">
              ⚡ Apply This Repo's Fix
            </button>
          </div>

          <div class="meta">
            <span class="repo-badge"><a href="${repoUrl}">${escapeHtml(r.repo)}</a></span>
            <span class="file-path">${fileEscaped}</span>
          </div>
        </div>
      `;
        }).join('');
        const buggyPreview = escapeHtml(buggyCode.length > 500 ? buggyCode.slice(0, 500) + '\n...' : buggyCode);
        const aiFixCode = generatedFix?.fixed_code ?? '';
        return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src https: data:;">
  <title>Bug Fix Recommendations</title>
  <style>
    :root {
      --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      --mono: "Cascadia Code", "Fira Code", Consolas, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: var(--font);
      font-size: 13px;
      color: var(--vscode-foreground);
      background: var(--vscode-editor-background);
      padding: 16px;
      line-height: 1.5;
    }
    h1 { font-size: 15px; font-weight: 600; margin-bottom: 4px; }
    .meta-header { font-size: 11px; color: var(--vscode-descriptionForeground); margin-bottom: 16px; }

    /* CodeT5 banner */
    .ai-banner {
      background: rgba(80, 200, 120, 0.08);
      border: 1px solid rgba(80, 200, 120, 0.4);
      border-radius: 6px;
      padding: 12px 14px;
      margin-bottom: 16px;
    }
    .ai-unavailable {
      background: rgba(255,165,0,0.08);
      border-color: rgba(255,165,0,0.4);
      font-size: 11px;
      color: var(--vscode-editorWarning-foreground, #ffa500);
    }
    .ai-title {
      font-weight: 700;
      font-size: 12px;
      margin-bottom: 8px;
      color: #4ade80;
    }
    .ai-badge {
      font-size: 10px;
      background: rgba(80,200,120,0.2);
      padding: 1px 6px;
      border-radius: 8px;
      margin-left: 6px;
      font-weight: normal;
    }
    .ai-code {
      font-family: var(--mono);
      font-size: 11px;
      background: var(--vscode-textCodeBlock-background);
      padding: 10px 12px;
      border-radius: 4px;
      white-space: pre-wrap;
      overflow-x: auto;
      max-height: 300px;
      overflow-y: auto;
      margin-bottom: 10px;
      color: var(--vscode-editor-foreground);
    }

    /* Query preview */
    .query-preview {
      background: var(--vscode-textBlockQuote-background);
      border-left: 3px solid var(--vscode-textBlockQuote-border);
      padding: 8px 12px;
      border-radius: 4px;
      margin-bottom: 16px;
    }
    .query-preview summary { font-size: 11px; color: var(--vscode-descriptionForeground); cursor: pointer; margin-bottom: 6px; }
    .query-preview pre { font-family: var(--mono); font-size: 11px; white-space: pre-wrap; word-break: break-all; max-height: 100px; overflow: auto; }

    /* Cards */
    .card { background: var(--vscode-editorWidget-background); border: 1px solid var(--vscode-panel-border); border-radius: 6px; padding: 12px 14px; margin-bottom: 12px; }
    .card-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; }
    .rank { font-size: 13px; font-weight: 700; color: var(--vscode-button-background); min-width: 28px; }
    .score-bar-wrap { flex: 1; height: 5px; background: var(--vscode-progressBar-background); border-radius: 3px; opacity: 0.4; }
    .score-bar { height: 100%; background: var(--vscode-button-background); border-radius: 3px; }
    .score-label { font-size: 11px; color: var(--vscode-descriptionForeground); min-width: 70px; text-align: right; }

    .diagnosis { background: var(--vscode-textBlockQuote-background); border-left: 3px solid var(--vscode-button-background); padding: 8px 12px; border-radius: 4px; margin-bottom: 10px; }
    .diagnosis-label, .section-label { font-weight: 700; font-size: 11px; display: block; margin-bottom: 4px; color: var(--vscode-button-background); }
    .diagnosis-text { font-size: 11px; }

    .diff-section { margin-bottom: 10px; }
    .diff-block { font-family: var(--mono); font-size: 11px; line-height: 1.6; border: 1px solid var(--vscode-panel-border); border-radius: 4px; overflow-x: auto; max-height: 300px; overflow-y: auto; background: var(--vscode-editor-background); }
    .diff-line-row { display: grid; grid-template-columns: 35px 35px 1fr; white-space: pre; }
    .diff-line-row.removed { background: rgba(255,80,80,0.12); }
    .diff-line-row.added { background: rgba(80,200,80,0.12); }
    .line-num { color: var(--vscode-editorLineNumber-foreground, #858585); text-align: right; padding-right: 8px; font-size: 10px; border-right: 1px solid var(--vscode-panel-border); user-select: none; }
    .line-num-spacer { border-right: 1px solid var(--vscode-panel-border); }
    .diff-code-text { padding-left: 8px; white-space: pre; color: var(--vscode-editor-foreground); }
    .diff-line-row.removed .diff-code-text { color: #f87171; }
    .diff-line-row.added .diff-code-text { color: #4ade80; }
    .diff-line-row.context { opacity: 0.7; }

    .full-fix { margin-bottom: 10px; }
    .full-fix summary { cursor: pointer; user-select: none; list-style: none; padding: 2px 0; }
    .full-fix summary::-webkit-details-marker { display: none; }
    .code-block { margin-top: 6px; background: var(--vscode-textCodeBlock-background); border: 1px solid var(--vscode-panel-border); border-radius: 4px; padding: 10px 12px; font-family: var(--mono); font-size: 11px; white-space: pre; overflow-x: auto; max-height: 300px; overflow-y: auto; color: var(--vscode-editor-foreground); line-height: 1.6; }

    .card-actions { margin-bottom: 10px; }
    .action-btn { padding: 6px 12px; font-size: 11px; font-weight: 500; border-radius: 4px; cursor: pointer; border: none; }
    .btn-primary { background: var(--vscode-button-background); color: var(--vscode-button-foreground); }
    .btn-primary:hover { background: var(--vscode-button-hoverBackground); }
    .btn-secondary { background: var(--vscode-button-secondaryBackground, rgba(255,255,255,0.08)); color: var(--vscode-foreground); border: 1px solid var(--vscode-panel-border); }
    .btn-secondary:hover { background: var(--vscode-button-secondaryHoverBackground, rgba(255,255,255,0.15)); }

    .meta { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; margin-top: 8px; }
    .repo-badge { font-size: 11px; background: var(--vscode-badge-background); color: var(--vscode-badge-foreground); padding: 1px 7px; border-radius: 10px; }
    .repo-badge a { color: var(--vscode-badge-foreground); text-decoration: none; }
    .repo-badge a:hover { text-decoration: underline; }
    .file-path { font-size: 10px; font-family: var(--mono); color: var(--vscode-descriptionForeground); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 300px; }
    .footer { font-size: 10px; color: var(--vscode-descriptionForeground); margin-top: 16px; text-align: center; }
  </style>
</head>
<body>

<h1>Bug Fix Recommendations</h1>
<p class="meta-header">
  ${response.total_results} results &nbsp;·&nbsp;
  ${response.query_time_ms.toFixed(0)}ms &nbsp;·&nbsp;
  Index: ${response.pairs_indexed.toLocaleString()} pairs
</p>

${codet5Banner}

<details class="query-preview">
  <summary>Your queried code (click to expand)</summary>
  <pre>${buggyPreview}</pre>
</details>

${cards}

<p class="footer">
  Powered by BM25 retrieval + CodeT5 local model · ${response.pairs_indexed.toLocaleString()} real GitHub bug-fix commits
</p>

<script>
  const vscode = acquireVsCodeApi();
  const bm25FixedCodes = ${JSON.stringify(response.results.map(r => r.fixed_code))};
  const aiFixCode = ${JSON.stringify(aiFixCode)};

  function applyAiFix() {
    vscode.postMessage({ command: 'applyFix', code: aiFixCode });
  }

  function applyBm25Fix(index) {
    vscode.postMessage({ command: 'applyFix', code: bm25FixedCodes[index] });
  }
</script>

</body>
</html>`;
    }
}
exports.ResultsPanel = ResultsPanel;
ResultsPanel.viewType = 'bugfixResults';
function escapeHtml(str) {
    return str
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#039;');
}
function runLcsDiff(buggy, fixed) {
    const a = buggy.split(/\r?\n/);
    const b = fixed.split(/\r?\n/);
    const m = a.length;
    const n = b.length;
    const dp = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0));
    for (let i = 1; i <= m; i++) {
        for (let j = 1; j <= n; j++) {
            if (a[i - 1].trim() === b[j - 1].trim()) {
                dp[i][j] = dp[i - 1][j - 1] + 1;
            }
            else {
                dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1]);
            }
        }
    }
    let i = m, j = n;
    const diff = [];
    while (i > 0 || j > 0) {
        if (i > 0 && j > 0 && a[i - 1].trim() === b[j - 1].trim()) {
            diff.push({ type: 'context', text: b[j - 1] });
            i--;
            j--;
        }
        else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
            diff.push({ type: 'added', text: b[j - 1] });
            j--;
        }
        else {
            diff.push({ type: 'removed', text: a[i - 1] });
            i--;
        }
    }
    return diff.reverse();
}
function buildInlineDiff(diffLines) {
    let buggyLineNum = 1;
    let fixedLineNum = 1;
    const capped = diffLines.slice(0, 60);
    const html = capped.map(line => {
        let prefix = '  ';
        let lineClass = 'context';
        let lineInfo = '';
        if (line.type === 'removed') {
            prefix = '− ';
            lineClass = 'removed';
            lineInfo = `<span class="line-num">${buggyLineNum++}</span><span class="line-num-spacer"></span>`;
        }
        else if (line.type === 'added') {
            prefix = '+ ';
            lineClass = 'added';
            lineInfo = `<span class="line-num-spacer"></span><span class="line-num">${fixedLineNum++}</span>`;
        }
        else {
            lineInfo = `<span class="line-num">${buggyLineNum++}</span><span class="line-num">${fixedLineNum++}</span>`;
        }
        return `<div class="diff-line-row ${lineClass}">${lineInfo}<span class="diff-code-text">${prefix}${escapeHtml(line.text)}</span></div>`;
    }).join('\n');
    return html;
}
//# sourceMappingURL=resultsPanel.js.map