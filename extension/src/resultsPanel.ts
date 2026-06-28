/**
 * resultsPanel.ts — WebView panel for displaying fix recommendations.
 *
 * WHY WebView: VS Code's native UI cannot display rich diffs or
 * multi-column layouts. WebView gives us full HTML/CSS control.
 *
 * Design decisions:
 *  - Single panel reused across calls (createOrShow pattern)
 *  - No external CSS/JS dependencies — all inline
 *  - Uses VS Code's CSS variables so it auto-adapts to dark/light theme
 *  - Scores shown as bars so developers see confidence at a glance
 *  - Commit message shown so developers understand WHY the fix was applied
 *  - Full fixed_code shown in a scrollable block (not truncated)
 */

import * as vscode from 'vscode';

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

export class ResultsPanel {
  public static currentPanel: ResultsPanel | undefined;
  private static readonly viewType = 'bugfixResults';

  private readonly _panel: vscode.WebviewPanel;
  private _disposables: vscode.Disposable[] = [];

  // ── Factory ────────────────────────────────────────────────

  public static createOrShow(
    extensionUri: vscode.Uri,
    response: RecommendResponse,
    buggyCode: string
  ): void {
    const column = vscode.window.activeTextEditor
      ? vscode.ViewColumn.Beside  // open beside the editor
      : vscode.ViewColumn.One;

    // Reuse existing panel if open
    if (ResultsPanel.currentPanel) {
      ResultsPanel.currentPanel._panel.reveal(column);
      ResultsPanel.currentPanel._update(response, buggyCode);
      return;
    }

    const panel = vscode.window.createWebviewPanel(
      ResultsPanel.viewType,
      'Bug Fix Recommendations',
      column,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(extensionUri, 'out')],
      }
    );

    ResultsPanel.currentPanel = new ResultsPanel(panel);
    ResultsPanel.currentPanel._update(response, buggyCode);
  }

  // ── Constructor ────────────────────────────────────────────

  private constructor(panel: vscode.WebviewPanel) {
    this._panel = panel;

    this._panel.onDidDispose(
      () => this._dispose(),
      null,
      this._disposables
    );
  }

  private _dispose(): void {
    ResultsPanel.currentPanel = undefined;
    this._panel.dispose();
    while (this._disposables.length) {
      const d = this._disposables.pop();
      if (d) { d.dispose(); }
    }
  }

  // ── Content update ─────────────────────────────────────────

  private _update(response: RecommendResponse, buggyCode: string): void {
    this._panel.title = `Bug Fix — ${response.total_results} results`;
    this._panel.webview.html = this._buildHtml(response, buggyCode);
  }

  // ── HTML builder ───────────────────────────────────────────

  private _buildHtml(response: RecommendResponse, buggyCode: string): string {
    const maxScore = Math.max(...response.results.map(r => r.score));

    const cards = response.results.map(r => {
      const pct = maxScore > 0 ? Math.round((r.score / maxScore) * 100) : 0;
      const repoUrl = `https://github.com/${r.repo}`;
      // Escape HTML to prevent XSS from code content
      const fixedEscaped = escapeHtml(extractDiff(r.buggy_code, r.fixed_code));
      const commitEscaped = escapeHtml(r.commit_message);
      const fileEscaped = escapeHtml(r.file_path);

      return `
        <div class="card">
          <div class="card-header">
            <span class="rank">Rank ${r.rank}</span>
            <div class="score-bar-wrap">
              <div class="score-bar" style="width:${pct}%"></div>
            </div>
            <span class="score-label">Score ${r.score.toFixed(2)}</span>
          </div>

          <div class="meta">
            <span class="repo-badge">
              <a href="${repoUrl}">${escapeHtml(r.repo)}</a>
            </span>
            <span class="file-path">${fileEscaped}</span>
          </div>

          <div class="commit-msg">
            <span class="label">Commit:</span> ${commitEscaped}
          </div>

          <details open>
            <summary>View suggested fix</summary>
            <pre class="code-block"><code>${fixedEscaped}</code></pre>
          </details>
        </div>
      `;
    }).join('');

    const buggyPreview = escapeHtml(
      buggyCode.length > 500 ? buggyCode.slice(0, 500) + '\n...' : buggyCode
    );

    return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="Content-Security-Policy"
        content="default-src 'none';
                 style-src 'unsafe-inline';
                 script-src 'unsafe-inline';
                 img-src https: data:;">
  <title>Bug Fix Recommendations</title>
  <style>
    /* Use VS Code's CSS variables — auto dark/light theme */
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

    h1 {
      font-size: 15px;
      font-weight: 600;
      margin-bottom: 4px;
      color: var(--vscode-foreground);
    }

    .meta-header {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      margin-bottom: 16px;
    }

    .query-preview {
      background: var(--vscode-textBlockQuote-background);
      border-left: 3px solid var(--vscode-textBlockQuote-border);
      padding: 8px 12px;
      border-radius: 4px;
      margin-bottom: 16px;
    }

    .query-preview summary {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      cursor: pointer;
      user-select: none;
      margin-bottom: 6px;
    }

    .query-preview pre {
      font-family: var(--mono);
      font-size: 11px;
      white-space: pre-wrap;
      word-break: break-all;
      color: var(--vscode-editor-foreground);
      max-height: 100px;
      overflow: auto;
    }

    .card {
      background: var(--vscode-editorWidget-background);
      border: 1px solid var(--vscode-panel-border);
      border-radius: 6px;
      padding: 12px 14px;
      margin-bottom: 12px;
    }

    .card-header {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 8px;
    }

    .rank {
      font-size: 11px;
      font-weight: 700;
      color: var(--vscode-button-background);
      min-width: 44px;
    }

    .score-bar-wrap {
      flex: 1;
      height: 5px;
      background: var(--vscode-progressBar-background);
      border-radius: 3px;
      opacity: 0.4;
    }

    .score-bar {
      height: 100%;
      background: var(--vscode-button-background);
      border-radius: 3px;
    }

    .score-label {
      font-size: 11px;
      color: var(--vscode-descriptionForeground);
      min-width: 70px;
      text-align: right;
    }

    .meta {
      display: flex;
      gap: 10px;
      align-items: center;
      margin-bottom: 6px;
      flex-wrap: wrap;
    }

    .repo-badge {
      font-size: 11px;
      background: var(--vscode-badge-background);
      color: var(--vscode-badge-foreground);
      padding: 1px 7px;
      border-radius: 10px;
    }

    .repo-badge a {
      color: var(--vscode-badge-foreground);
      text-decoration: none;
    }

    .repo-badge a:hover { text-decoration: underline; }

    .file-path {
      font-size: 10px;
      font-family: var(--mono);
      color: var(--vscode-descriptionForeground);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
      max-width: 300px;
    }

    .commit-msg {
      font-size: 11px;
      color: var(--vscode-foreground);
      margin-bottom: 8px;
      line-height: 1.4;
    }

    .label {
      font-weight: 600;
      color: var(--vscode-descriptionForeground);
    }

    details { margin-top: 6px; }

    details summary {
      font-size: 11px;
      color: var(--vscode-textLink-foreground);
      cursor: pointer;
      user-select: none;
      outline: none;
    }

    details summary:hover { text-decoration: underline; }

    .code-block {
      margin-top: 8px;
      background: var(--vscode-textCodeBlock-background);
      border: 1px solid var(--vscode-panel-border);
      border-radius: 4px;
      padding: 10px 12px;
      font-family: var(--mono);
      font-size: 11px;
      white-space: pre;
      overflow-x: auto;
      max-height: 400px;
      overflow-y: auto;
      color: var(--vscode-editor-foreground);
      line-height: 1.6;
    }

    .footer {
      font-size: 10px;
      color: var(--vscode-descriptionForeground);
      margin-top: 16px;
      text-align: center;
    }
  </style>
</head>
<body>

<h1>Bug Fix Recommendations</h1>
<p class="meta-header">
  ${response.total_results} results &nbsp;·&nbsp;
  ${response.query_time_ms.toFixed(0)}ms &nbsp;·&nbsp;
  Index: ${response.pairs_indexed.toLocaleString()} pairs
</p>

<details class="query-preview">
  <summary>Your queried code (click to expand)</summary>
  <pre>${buggyPreview}</pre>
</details>

${cards}

<p class="footer">
  Powered by BM25 retrieval over 11,883 real GitHub bug-fix commits
</p>

</body>
</html>`;
  }
}

// ── Utility ────────────────────────────────────────────────────

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}

function extractDiff(buggy: string, fixed: string): string {
  const buggyLines = buggy.split('\n');
  const fixedLines = fixed.split('\n');
  
  const removed: string[] = [];
  const added: string[] = [];
  
  // Find lines that changed
  const buggySet = new Set(buggyLines.map(l => l.trim()));
  const fixedSet = new Set(fixedLines.map(l => l.trim()));
  
  for (const line of buggyLines) {
    if (!fixedSet.has(line.trim()) && line.trim()) {
      removed.push('- ' + line);
    }
  }
  for (const line of fixedLines) {
    if (!buggySet.has(line.trim()) && line.trim()) {
      added.push('+ ' + line);
    }
  }
  
  if (removed.length === 0 && added.length === 0) {
    return fixed.slice(0, 300);
  }
  return [...removed, ...added].slice(0, 30).join('\n');
}